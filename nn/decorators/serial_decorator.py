from typing import List
from jax.typing import ArrayLike
from jax.random import PRNGKey
from nn.typing import Params
from nn.debug_utils import *
import logging
import time
import jax
import functools

def debug_decorator(serial_debug):
    """
    Decorator to print debug information of the forward pass for INFO2 log level.
    """
    @functools.wraps(serial_debug)
    def serial(*args, **kwargs):
        if logging.getLevelName(logging.root.level) == "INFO2":
            init_fun_debug, apply_fun_debug = serial_debug(*args, **kwargs)

            @functools.wraps(init_fun_debug)
            def init_fun(rng: PRNGKey, input_shape):
                jax.debug.print("\n=== Start Init Fun Execution ===")
                start_time_forward = time.time()

                output_shape, params = init_fun_debug(rng, input_shape)

                end_time_forward = time.time()
                time_elapsed_ms = (end_time_forward - start_time_forward)
                jax.debug.print("Serial Initialization Took: {:.2f} seconds", time_elapsed_ms)
                jax.debug.print("=== End Init Fun Execution ===\n")
                jax.debug.breakpoint()

                # Print parameters in a much cleaner way.
                # print_params(params)
                jax.debug.breakpoint()

                return output_shape, params
            
            @functools.wraps(apply_fun_debug)
            def apply_fun(params: List[Params], inputs: ArrayLike, **kwargs):
                if logging.getLevelName(logging.root.level) == "INFO2":
                    # Before Function Execution
                    jax.debug.print("\n=== Start Serial Forward Pass Execution ===")
                    start_time_forward = time.time()

                    result = apply_fun_debug(params, inputs, **kwargs)

                    # After Function Execution
                    end_time_forward = time.time()
                    time_elapsed_ms = (end_time_forward - start_time_forward) * 1000
                    jax.debug.print("Forward Pass Took: {:.2f} ms", time_elapsed_ms)
                    jax.debug.print("=== End Serial Forward Pass Execution ===\n")
                    jax.debug.breakpoint()
                    # returning the value to the original frame
                    return result
                else:
                    return apply_fun_debug(params, inputs, **kwargs)

            return init_fun, apply_fun
        else:
            return serial_debug(*args, **kwargs)
        
    return serial