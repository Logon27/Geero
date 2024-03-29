"""A Resnet example for CIFAR-10. 

It achieves around 90% accuracy on the test set.
Uses rotation, resizing/cropping, and flipping for data augmentation
The adam optimizer is used with an exponential decay learning rate schedule
"""

import sys
sys.path.append("..")

# Import the TQDM config for cleaner progress bars
import training_examples.helpers.tqdm_config # pyright: ignore
from tqdm import trange

import itertools
import jax.numpy as jnp
from jax import jit, grad, random
from jax.tree_util import (tree_map, tree_flatten)
import training_examples.helpers.datasets as datasets
import matplotlib.pyplot as plt
from matplotlib.widgets import Button
from dm_pix import rotate, random_flip_left_right, resize_with_crop_or_pad, random_crop
from nn import *

# ResNet blocks compose other layers
def ConvBlock(kernel_size, filters, strides=(2, 2)):
    ks = kernel_size
    filters1, filters2, filters3 = filters
    Main = serial(
        Conv(filters1, (1, 1), strides),
        BatchNorm(),
        Relu,
        Conv(filters2, (ks, ks), padding="SAME"),
        BatchNorm(),
        Relu,
        Conv(filters3, (1, 1)),
        BatchNorm(),
    )
    Shortcut = serial(Conv(filters3, (1, 1), strides), BatchNorm())
    return serial(FanOut(2), parallel(Main, Shortcut), FanInSum, Relu)

def IdentityBlock(kernel_size, filters):
    ks = kernel_size
    filters1, filters2 = filters

    def make_main(input_shape):
        # the number of output channels depends on the number of input channels
        return serial(
            Conv(filters1, (1, 1)),
            BatchNorm(),
            Relu,
            Conv(filters2, (ks, ks), padding="SAME"),
            BatchNorm(),
            Relu,
            Conv(input_shape[3], (1, 1)),
            BatchNorm(),
        )

    Main = shape_dependent(make_main)
    return serial(FanOut(2), parallel(Main, Identity), FanInSum, Relu)


# https://medium.com/analytics-vidhya/understanding-and-implementation-of-residual-networks-resnets-b80f9a507b9c
def ResNet(num_classes):
  return serial(
        Conv(64, (3, 3), (1, 1), padding="SAME"),
        BatchNorm(), Relu,
        IdentityBlock(3, [64, 64]),
        IdentityBlock(3, [64, 64]),
        ConvBlock(3, [64, 64, 128]),
        IdentityBlock(3, [128, 128]),
        IdentityBlock(3, [128, 128]),
        ConvBlock(3, [128, 128, 256]),
        IdentityBlock(3, [256, 256]),
        IdentityBlock(3, [256, 256]),
        AvgPool((8, 8)),
        Flatten,
        Dense(num_classes),
        LogSoftmax
    )

#   def init_fun(rng, input_shape):
#     return make_layer(input_shape)[0](rng, input_shape)
# For the above 'make_layer' is actually the 'make_main' function.
# So only the input shape is passed in from the init_fun parameters.
# Which resolves the stax.serial of 'make_main'. And finally the rng and input_shape are passed
# into the resulting stax.serial which is returned by make_main.
# This is necessary because we will not know the output shape of the identity block's previous layer until
# the serial function does its init_fun execution to calculate the output sizes. This is because convolutional layers
# only specify the number of output channels in the parameters and not their output shapes.


def accuracy(params, states, batch, rng):
    inputs, targets = batch
    target_class = jnp.argmax(targets, axis=1)
    predicted_class = jnp.argmax(net_predict(params, states, inputs, rng=rng, mode="test")[0], axis=1)
    return jnp.mean(predicted_class == target_class)

# (128, 32, 32, 3)
@jit
def augment(rng, batch):
    # Generate the same number of keys as the array size.
    subkeys = random.split(rng, batch.shape[0])
    batch = batch * 255
    # Randomly rotate the image https://dm-pix.readthedocs.io/en/latest/api.html#rotate
    random_angles = jax.vmap(lambda x: jax.random.uniform(x, minval=-15, maxval=15), in_axes=(0), out_axes=0)(subkeys)
    batch = jax.vmap(lambda array, angle : rotate(array, angle=(angle * (jnp.pi / 180))))(batch, random_angles)
    # Resize to 36x36 then randomly crop the images back to 32x32
    batch = jax.vmap(lambda array : resize_with_crop_or_pad(array, 36, 36, channel_axis=2))(batch)
    batch = jax.vmap(lambda array, key : random_crop(key, array, (32, 32, 3)))(batch, subkeys)
    # Randomly flip the image
    batch = jax.vmap(lambda array, key : random_flip_left_right(key, array))(batch, subkeys)
    batch = batch / 255
    return batch

num_classes = 10
net_init, net_predict = model_decorator(ResNet(num_classes))

def main():
    rng = random.PRNGKey(0)

    step_size = 0.001
    num_epochs = 40
    batch_size = 128
    # IMPORTANT
    # If your network is larger and you test against the entire dataset for the accuracy.
    # Then you will run out of RAM and get a std::bad_alloc error.
    accuracy_batch_size = 1000
    grad_clip = 1.0

    train_images, train_labels, test_images, test_labels = datasets.cifar10()
    num_train = train_images.shape[0]
    num_complete_batches, leftover = divmod(num_train, batch_size)
    num_batches = num_complete_batches + bool(leftover)

    # Learning rate schedule that introduces exponential decay
    # https://keras.io/api/optimizers/learning_rate_schedules/exponential_decay/
    def exponential_decay(initial_learning_rate, decay_rate, decay_steps):
        def schedule(step):
            return initial_learning_rate * decay_rate ** (step / decay_steps)
        return schedule

    # https://github.com/google/jax/blob/7961fb81cf7643387c472ad51881332379f2893c/jax/example_libraries/optimizers.py#L571
    def l2_norm(tree):
        """Compute the l2 norm of a pytree of arrays. Useful for weight decay."""
        leaves, _ = tree_flatten(tree)
        return jnp.sqrt(sum(jnp.vdot(x, x) for x in leaves))

    def clip_grads(grad_tree, max_norm):
        """Clip gradients stored as a pytree of arrays to maximum norm `max_norm`."""
        norm = l2_norm(grad_tree)
        normalize = lambda g: jnp.where(norm < max_norm, g, g * (max_norm / norm))
        return tree_map(normalize, grad_tree)

    def data_stream(rng):
        while True:
            rng, subkey = random.split(rng)
            perm = random.permutation(subkey, num_train)
            for i in range(num_batches):
                # batch_idx is a list of indices.
                # That means this function yields an array of training images equal to the batch size when 'next' is called.
                batch_idx = perm[i * batch_size : (i + 1) * batch_size]
                rng, subkey = random.split(rng)
                yield augment(subkey, train_images[batch_idx]), train_labels[batch_idx]

    batches = data_stream(rng)
    # 0.001 * (0.96 ^ ((num_batches * epochs) / 300))
    opt_init, opt_update, get_params = adam(exponential_decay(step_size, 0.96, 300))

    @jit
    def update(i, opt_state, states, batch):
        def loss(params, states, batch):
            """Calculates the loss of the network as a single value / float"""
            inputs, targets = batch
            predictions, states = net_predict(params, states, inputs, rng=rng)
            return categorical_cross_entropy(predictions, targets), states

        params = get_params(opt_state)
        grads, states = grad(loss, has_aux=True)(params, states, batch)
        # Clip gradients to prevent the exploding gradients
        grads = clip_grads(grads, grad_clip)
        return opt_update(i, grads, opt_state), states
    
    _, init_params, states = net_init(rng, (1, 32, 32, 3))
    opt_state = opt_init(init_params)
    itercount = itertools.count()

    print("Starting training...")
    highest_train_acc = 0
    highest_test_acc = 0
    highest_opt_state, highest_states = opt_state, states
    for epoch in (t := trange(num_epochs)):
        for batch in range(num_batches):
            opt_state, states = update(next(itercount), opt_state, states, next(batches))

        params = get_params(opt_state)
        train_acc = accuracy(params, states, (train_images[:accuracy_batch_size], train_labels[:accuracy_batch_size]), rng)
        test_acc = accuracy(params, states, (test_images[:accuracy_batch_size], test_labels[:accuracy_batch_size]), rng)
        # Track the highest accuracy achieved
        if train_acc > highest_train_acc:
            highest_train_acc = train_acc
        if test_acc > highest_test_acc:
            # Save the highest weights for predictions
            highest_test_acc = test_acc
            highest_opt_state, highest_states = opt_state, states
        t.set_description_str("Accuracy Train = {:.2%}, Accuracy Test = {:.2%}".format(train_acc, test_acc))
    print("Training Complete.")
    print(f"Highest Train Accuracy {highest_train_acc:.2%}")
    print(f"Highest Test Accuracy {highest_test_acc:.2%}")
    print("Setting weights to highest achieved test accuracy")
    opt_state = highest_opt_state
    states = highest_states

    # Visual Debug After Training
    visual_debug(get_params(opt_state), states, test_images, test_labels, rng)

def visual_debug(params, states, test_images, test_labels, rng, starting_index=0, rows=5, columns=10):
    """Visually displays a number of images along with the network prediction. Green means a correct guess. Red means an incorrect guess"""
    print("Displaying Visual Debug...")

    cifar_dict = {
        0: "Airplane",
        1: "Automobile",
        2: "Bird",
        3: "Cat",
        4: "Deer",
        5: "Dog",
        6: "Frog",
        7: "Horse",
        8: "Ship",
        9: "Truck",
    }

    fig, axes = plt.subplots(nrows=rows, ncols=columns, sharex=False, sharey=True, figsize=(12, 8))
    # Set a bottom margin to space out the buttons from the figures
    fig.subplots_adjust(bottom=0.15)
    fig.canvas.manager.set_window_title('Network Predictions')
    class Index:
        def __init__(self, starting_index):
            self.starting_index = starting_index
        
        def render_images(self):
            i = self.starting_index
            for j in range(rows):
                for k in range(columns):
                    output = net_predict(params, states, test_images[i].reshape(1, *test_images[i].shape), rng=rng, mode="test")[0]
                    prediction = int(jnp.argmax(output, axis=1)[0])
                    target = int(jnp.argmax(test_labels[i], axis=0))
                    prediction_color = "green" if prediction == target else "red"
                    axes[j][k].set_title(cifar_dict[prediction], fontsize = 10, color=prediction_color)
                    axes[j][k].imshow(test_images[i])
                    axes[j][k].get_xaxis().set_visible(False)
                    axes[j][k].get_yaxis().set_visible(False)
                    i += 1
            plt.draw()
            fig.suptitle("Displaying Images: {} - {}".format(self.starting_index, (self.starting_index + (rows * columns))), fontsize=14)
        
        def next(self, event):
            self.starting_index += (rows * columns)
            self.render_images()
        
        def prev(self, event):
            self.starting_index -= (rows * columns)
            self.render_images()

    callback = Index(starting_index)
    axprev = fig.add_axes([0.7, 0.05, 0.1, 0.075])
    axnext = fig.add_axes([0.81, 0.05, 0.1, 0.075])
    bnext = Button(axnext, 'Next', hovercolor="green")
    bnext.on_clicked(callback.next)
    bprev = Button(axprev, 'Previous', hovercolor="green")
    bprev.on_clicked(callback.prev)
    # Run an initial render before buttons are pressed
    callback.render_images()
    plt.show()

if __name__ == "__main__":
    main()