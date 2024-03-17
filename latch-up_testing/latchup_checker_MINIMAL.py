#! /usr/bin/env python3

import argparse
import errno
import fcntl
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import os
from pydwf import (DwfLibrary,
                   DwfEnumConfigInfo,
                   DwfAnalogOutNode,
                   DwfAnalogOutFunction,
                   DwfAcquisitionMode,
                   DwfAnalogInFilter,
                   PyDwfError)
from pydwf.utilities import openDwfDevice
import socket
import sys
import time
from trbnet import TrbNet


F_SETPIPE_SZ = 1031  # Linux 2.6.35+
F_GETPIPE_SZ = 1032  # Linux 2.6.35+


# def mypause(interval):
#     backend = plt.rcParams['backend']
#     if backend in matplotlib.rcsetup.interactive_bk:
#         figManager = matplotlib._pylab_helpers.Gcf.get_active()
#         if figManager is not None:
#             canvas = figManager.canvas
#             if canvas.figure.stale:
#                 canvas.draw()
#             canvas.start_event_loop(interval)
#             return


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


maskV = 0x3
maskO = 0x4


def run(analogIn,
        digitalIO,
        samplingFreq,
        inputRange,
        minDiff,
        polarityDiff,
        searchInt,
        pipe,
        trb,
        sock):

    CH1 = 0
    channels = (CH1,)

    for channel_index in channels:
        analogIn.channelEnableSet(channel_index, True)
        analogIn.channelFilterSet(channel_index, DwfAnalogInFilter.Decimate)
        analogIn.channelRangeSet (channel_index, inputRange)

    acquisition_mode = DwfAcquisitionMode.ScanScreen
    analogIn.acquisitionModeSet(acquisition_mode)
    analogIn.frequencySet(samplingFreq)
    analogIn.configure(False, True)  # Start acquisition sequence.
    num_samples = analogIn.bufferSizeGet()

    # first acquisition
    analogIn.status(True)
    c1 = analogIn.statusData(CH1, num_samples)
    writeIndex = analogIn.statusIndexWrite()
    lastWriteIndex = writeIndex
    beforeLastIndex = 0

    fig, ax1 = plt.subplots()
    ax1.set_title("acquisition mode: {}".format(acquisition_mode.name))
    ax1.set_xlabel("samples [-]")
    ax1.set_ylabel("signals [V]")
    ax1.set_ylim(-inputRange, +inputRange)
    (p1, ) = ax1.plot(c1, '.', label="CH1")

    plt.show(block=False)

    vline = plt.axvline(0, c='grey')
    vline.set_xdata([writeIndex])


    def search_latch_up(c, offset, pipe,s):

        skipCounter = 0

        for x in range(len(c)-searchInt):

            diff = abs(c[x+searchInt] - c[x])
            # diff = c[x+searchInt] - c[x]
            offset = offset if offset+x <= num_samples else offset+x-num_samples

            overCurrent = 0.135

            if c[x] >= overCurrent: #latchup condition!

                # print(c[x+searchInt] - c[x])

                digitalIO.outputSet(0b111) # override pressed
                time.sleep(0.1)

                # Turn supply OFF
                s.send(b"INST OUTP1\r\nOUTP:SEL OFF\r\nOUTP?\r\n")
                while int(s.recv(4096)) != 0:
                    s.send(b"OUTP:SEL OFF\r\nOUTP?\r\n")

                lo = 0 if x-searchInt < 0 else x-searchInt
                try:
                    os.write(pipe,c[lo:x+searchInt].tobytes())
                except Exception as exception:
                    if exception.errno == errno.EAGAIN:
                        print("Waiting for pipe reader to consume data...")
                        time.sleep(0.5)
                    else:
                        print("Pipe closed, waiting on re-opening...")
                        pipe = open_pipe(pipe) # wait until pipe is open again

                time.sleep(.1)
                digitalIO.outputSet(0b011) # all released

                time.sleep(2)

                # Turn ON
                s.send(b"INST OUTP1\r\nOUTP:SEL ON\r\nOUTP?\r\n")
                while int(s.recv(4096)) != 1:
                    s.send(b"OUTP:SEL ON\r\nOUTP?\r\n")

                time.sleep(0.5)

                print("Over " + str(overCurrent) + " " + str(offset+x))

                x=digitalIO.inputStatus()
                while x & 0b11000 != 0b11000:
                    # digitalIO.outputSet(0b011) # all released
                    # time.sleep(0.1)
                    digitalIO.outputSet(0b111) # override pressed
                    time.sleep(0.1)
                    digitalIO.outputSet(0b101) # override together with analog
                    time.sleep(0.1)
                    digitalIO.outputSet(0b111) # ovverride pressed
                    time.sleep(0.1)
                    digitalIO.outputSet(0b110) # override with digital
                    time.sleep(0.1)
                    digitalIO.outputSet(0b111) # ovverride pressed
                    time.sleep(0.1)
                    digitalIO.outputSet(0b011) # all released
                    time.sleep(1)
                    x=digitalIO.inputStatus()

                #trbcmd w 0xfe82 0xde05 0x100  #Mimosis reset
                # trb.register_write(0xa000, 0xde05, 0x100)
                return True

            # elif diff < -minDiff: #latchup condition!
            # elif diff > minDiff or c[x] >= 0.2: #latchup condition!
            elif diff > minDiff:

                print(c[x+searchInt] - c[x])

                # Turn supply OFF
                s.send(b"INST OUTP1\r\nOUTP:SEL OFF\r\nOUTP?\r\n")
                while int(s.recv(4096)) != 0:
                    s.send(b"OUTP:SEL OFF\r\nOUTP?\r\n")

                lo = 0 if x-searchInt < 0 else x-searchInt
                try:
                    # os.write(pipe,c[lo:x+searchInt].tobytes())
                    os.write(pipe,c[lo:-1].tobytes())
                except Exception as exception:
                    if exception.errno == errno.EAGAIN:
                        print("Waiting for pipe reader to consume data...")
                        time.sleep(0.5)
                    else:
                        print("Pipe closed, waiting on re-opening...")
                        pipe = open_pipe(pipe) # wait until pipe is open again

                time.sleep(2)

                # Turn ON
                s.send(b"INST OUTP1\r\nOUTP:SEL ON\r\nOUTP?\r\n")
                while int(s.recv(4096)) != 1:
                    s.send(b"OUTP:SEL ON\r\nOUTP?\r\n")

                time.sleep(0.5)

                print("Latchup detected " + str(offset+x))

                x=digitalIO.inputStatus()
                while x & 0b11000 != 0b11000:
                    # digitalIO.outputSet(0b011) # all released
                    # time.sleep(0.1)
                    digitalIO.outputSet(0b111) # override pressed
                    time.sleep(0.1)
                    digitalIO.outputSet(0b101) # override together with analog
                    time.sleep(0.1)
                    digitalIO.outputSet(0b111) # ovverride pressed
                    time.sleep(0.1)
                    digitalIO.outputSet(0b110) # override with digital
                    time.sleep(0.1)
                    digitalIO.outputSet(0b111) # ovverride pressed
                    time.sleep(0.1)
                    digitalIO.outputSet(0b011) # all released
                    time.sleep(1)
                    x=digitalIO.inputStatus()

                #trbcmd w 0xfe82 0xde05 0x100  #Mimosis reset
                # trb.register_write(0xa000, 0xde05, 0x100)
                return True

        return False

    pipe = open_pipe(pipe)

    latchup = False
    latchupCounter = 0

    while True:

        analogIn.status(True)
        st = analogIn.statusRecord()
        if st[1] != 0 or st[2] != 0:
            print(st)
        c1 = analogIn.statusData(CH1, num_samples)
        writeIndex = analogIn.statusIndexWrite()-1
        # samplesValid = analogIn.statusSamplesValid()

        if latchup == False or latchupCounter >= 1:

            latchupCounter = 0

            # if writeIndex > lastWriteIndex and lastWriteIndex > beforeLastIndex and not latchup:
            if writeIndex > lastWriteIndex and lastWriteIndex > beforeLastIndex:

                latchup = search_latch_up(np.concatenate((
                    [c1[beforeLastIndex]],
                    c1[lastWriteIndex:writeIndex]), axis=None),beforeLastIndex,pipe,sock)

                # elif writeIndex < lastWriteIndex and lastWriteIndex > beforeLastIndex and not latchup:
            elif writeIndex < lastWriteIndex and lastWriteIndex > beforeLastIndex:

                latchup = search_latch_up(np.concatenate((
                    [c1[beforeLastIndex]],
                    c1[lastWriteIndex:num_samples],
                    c1[0:writeIndex],), axis=None), beforeLastIndex,pipe,sock)

                # elif writeIndex > lastWriteIndex and lastWriteIndex < beforeLastIndex and not latchup:
            elif writeIndex > lastWriteIndex and lastWriteIndex < beforeLastIndex:

                latchup = search_latch_up(np.concatenate((
                    [c1[beforeLastIndex]],
                    c1[0:lastWriteIndex],
                    c1[lastWriteIndex:writeIndex],), axis=None), beforeLastIndex,pipe,sock)
        else:
            latchupCounter += 1

        # p1.set_ydata(c1)
        # vline.set_xdata(writeIndex)
#        mypause(1e-3)
        # plt.pause(1e-3)

        beforeLastIndex = writeIndex - searchInt if writeIndex - searchInt >= 0 else num_samples - abs(writeIndex-searchInt)
        lastWriteIndex = writeIndex

        # User has closed the window, finish.
        if len(plt.get_fignums()) == 0:
            break


def main():

    parser = argparse.ArgumentParser(
        prog='latchup_checker_MINIMAL.py',
        description='Log Latch-ups',
        epilog='Good luck')

    parser.add_argument('-f', '--freq', default=20000,
                        help='Sampling frequency. Ignored if --time is provided. Defaults to 20000')
    parser.add_argument('-t', '--time',
                        help='Sample time. Has precedence over --freq. Defaults to 50mu')
    parser.add_argument('-r', '--range', default=5.0,
                        help='Oscis input range. Defaults to 5.0 V')
    parser.add_argument('-l', '--diff', default=1.0,
                        help='Valtage difference for trigger. Defaults to 1.0 V')
    parser.add_argument('-s', '--interval', default=1,
                        help='Find voltage difference within INTERVAL samples. Defaults to 1')
    parser.add_argument('-p', '--polarity',
                        help='Trigger on voltage increase (1) or decrease (2). If not provided, trigger on both.')

    args = parser.parse_args()

    samplingFreq = float(args.freq) if args.time == None else 1/args.time
    inputRange = float(args.range)
    minDiff = float(args.diff)
    polarityDiff = int(args.polarity) if args.polarity != None else None
    searchInt = int(args.interval)

    try:
        with openDwfDevice(DwfLibrary(), score_func=lambda c : c[DwfEnumConfigInfo.AnalogInBufferSize]) as device:

            analogIn  = device.analogIn

            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(('192.168.0.61', 5025))

            digitalIO = device.digitalIO
            digitalIO.reset()

            digitalIO.outputEnableSet(maskV + maskO)

            digitalIO.outputSet(0b100) # all pressed
            time.sleep(0.1)
            digitalIO.outputSet(0b111) # override pressed
            time.sleep(0.1)
            digitalIO.outputSet(0b011) # all released


            # digitalIO.outputSet(0b111) # override pressed
            # time.sleep(0.1)
            # digitalIO.outputSet(0b101) # override together with analog
            # time.sleep(0.1)
            # digitalIO.outputSet(0b111) # ovverride pressed
            # time.sleep(0.1)
            # digitalIO.outputSet(0b110) # override with digital
            # time.sleep(0.1)
            # digitalIO.outputSet(0b111) # ovverride pressed
            # time.sleep(0.1)
            # digitalIO.outputSet(0b011) # all released
            # time.sleep(0.1)

            # digitalIO.outputSet(0b111) # override pressed
            # time.sleep(0.1)

            # print("Turn on again")

            # # Turn supply OFF
            # s.send(b"INST OUTP1\r\nOUTP:SEL OFF\r\nOUTP?\r\n")
            # while int(s.recv(4096)) != 0:
            #     s.send(b"OUTP:SEL OFF\r\nOUTP?\r\n")

            # time.sleep(2)

            # # Turn ON
            # s.send(b"INST OUTP1\r\nOUTP:SEL ON\r\nOUTP?\r\n")
            # while int(s.recv(4096)) != 1:
            #     s.send(b"OUTP:SEL ON\r\nOUTP?\r\n")

            # time.sleep(0.5)
            # digitalIO.outputSet(0b011) # all released

            # print("Done")

            # sys.exit()



            pipe=None

            lib = '/home/xmatter/git/trbnettools/trbnetd/libtrbnet.so'
            host = 'localhost'
            t = TrbNet(libtrbnet=lib, daqopserver=host)


            run(
                analogIn,
                digitalIO,
                samplingFreq,
                inputRange,
                minDiff,
                polarityDiff,
                searchInt,
                pipe,
                t,
                s
            )

    except PyDwfError as exception:
        print("PyDwfError:", exception)


if __name__ == "__main__":
    main()
