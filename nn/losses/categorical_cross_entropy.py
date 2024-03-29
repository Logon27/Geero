import jax.numpy as jnp
from jax.typing import ArrayLike
from jax import Array


# https://github.com/google/jax/issues/1023#issuecomment-511822036
def categorical_cross_entropy(predictions: ArrayLike, targets: ArrayLike) -> Array:
    target_class = jnp.argmax(targets, axis=1)
    negative_log_likelihood = jnp.take_along_axis(predictions, jnp.expand_dims(target_class, axis=1), axis=1)
    cross_entropy = -jnp.mean(negative_log_likelihood)
    return cross_entropy