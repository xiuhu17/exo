from __future__ import annotations

from exo import *
from exo.stdlib.scheduling import *


@proc
def tile_and_fused_blur(
    W: size, H: size, blur_y: ui16[H, W] @ DRAM, inp: ui16[H + 2, W + 2] @ DRAM
):
    assert H % 32 == 0
    assert W % 256 == 0
    blur_x: ui16[2 + H, W] @ DRAM
    for yo in seq(0, H / 32):
        for xo in seq(0, W / 256):
            for yi in seq(0, 34):
                for xi in seq(0, 256):
                    blur_x[yi + 32 * yo, xi + 256 * xo] = (
                        inp[yi + 32 * yo, xi + 256 * xo]
                        + inp[yi + 32 * yo, 1 + xi + 256 * xo]
                        + inp[yi + 32 * yo, 2 + xi + 256 * xo]
                    ) / 3.0
            for yi in seq(0, 32):
                for xi in seq(0, 256):
                    blur_y[yi + 32 * yo, xi + 256 * xo] = (
                        blur_x[yi + 32 * yo, xi + 256 * xo]
                        + blur_x[1 + yi + 32 * yo, xi + 256 * xo]
                        + blur_x[2 + yi + 32 * yo, xi + 256 * xo]
                    ) / 3.0


def correct_schedule(p):
    def get_loops_at_or_above(cursor):
        loops = [cursor]
        while not isinstance((parent := cursor.parent()), InvalidCursor):
            loops.append(parent)
            cursor = parent
        return list(reversed(loops))

    xo_loop = p.find_loop("xo")
    producer_alloc = p.find("blur_x : _")

    loops_to_lower_allocation_into = get_loops_at_or_above(xo_loop)
    for loop in loops_to_lower_allocation_into:
        # Forward cursors before using
        loop = p.forward(loop)
        producer_alloc = p.forward(producer_alloc)

        p = sink_alloc(p, producer_alloc)

    return p


def wrong_schedule(p):
    """
    Incorrect function get_loops_at_or_above is missing the initial loop
    when initiating the loops array
    """

    def get_loops_at_or_above(cursor):
        loops = []
        while not isinstance((parent := cursor.parent()), InvalidCursor):
            loops.append(parent)
            cursor = parent
        return list(reversed(loops))

    xo_loop = p.find_loop("xo")
    producer_alloc = p.find("blur_x : _")

    loops_to_lower_allocation_into = get_loops_at_or_above(xo_loop)
    for loop in loops_to_lower_allocation_into:
        # Forward cursors before using
        loop = p.forward(loop)
        producer_alloc = p.forward(producer_alloc)

        p = sink_alloc(p, producer_alloc)

    return p


print(correct_schedule(tile_and_fused_blur))
print(wrong_schedule(tile_and_fused_blur))