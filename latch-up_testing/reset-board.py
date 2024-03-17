#!/usr/bin/env python

from pydwf import (DwfLibrary, DwfEnumConfigInfo, DwfAnalogOutNode, DwfAnalogOutFunction, DwfAcquisitionMode,
                   DwfAnalogInFilter, PyDwfError)
from pydwf.utilities import openDwfDevice
import time


def main():
    dwf = DwfLibrary()
    try:
        with openDwfDevice(dwf) as device:

            digitalIO = device.digitalIO
            digitalIO.reset()

            mask = 0xffff
            digitalIO.outputEnableSet(0xffff)
            digitalIO.pullSet(mask,0)

            while 1:
                digitalIO.outputSet(0xffff)

                time.sleep(1.0)

                digitalIO.outputSet(0x0)

                time.sleep(1.0)

            #digitalIO.pullSet(mask,0)
            # print(digitalIO.pullGet())

            time.sleep(2.0)

    except PyDwfError as exception:
        print("PyDwfError:", exception)
    # except DwfLibraryError as exception:
    #     print("DwfLibraryError:", exception)

if __name__ == "__main__":
    main()
