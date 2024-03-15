#! /usr/bin/env python3

import os
import time
import random
import numpy as np
import errno
import fcntl

num_samples = 17000 # actually 16xxx ... something

pipe = None


F_SETPIPE_SZ = 1031  # Linux 2.6.35+
F_GETPIPE_SZ = 1032  # Linux 2.6.35+

def open_pipe(pipe):
    while True:
        try:
            pipe = os.open('/tmp/latch_fifo', os.O_WRONLY | os.O_NONBLOCK)
        except OSError as ex:
            # reader has not opened the FIFO pipe yet
            if ex.errno == errno.ENXIO:
                time.sleep(1)
            else:
                print("Error opening FIFO pipe:", ex)
                break
        else:
            print("FIFO pipe opened successfully")
            break
    print("Original pipe size:", fcntl.fcntl(pipe, F_GETPIPE_SZ))
    fcntl.fcntl(pipe, F_SETPIPE_SZ, 1000000)
    print("Modified pipe size:", fcntl.fcntl(pipe, F_GETPIPE_SZ))
    return pipe

print("Waiting for FIFO pipe to open by reader...")

pipe = open_pipe(pipe)

while True:
    #latch_waveform = 5 * np.random.random_sample(num_samples) # scale to 0.0 .. +5.0

    steps = np.random.normal(0, 0.1, num_samples)

    # Generate the y values as a cumulative sum of the steps, like a random walk
    latch_waveform = np.cumsum(steps)


    time.sleep(random.randint(1,5))
    try:
        os.write(pipe,latch_waveform.tobytes())
    except Exception as exception:
        if exception.errno == errno.EAGAIN:
            print("Waiting for pipe reader to consume data...")
            time.sleep(0.5)
        else:
            print("Pipe closed, waiting on re-opening...")
            pipe = open_pipe(pipe) # wait until pipe is open again
    else:
        print("Random latch waveform sent")    