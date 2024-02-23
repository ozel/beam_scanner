#!/usr/bin/env python3
import logging
from microbeam.microbeam_web import MicrobeamWebInterface
from microbeam.microbeam_run_controller import MicrobeamRunController
from microbeam.microbeam_interface_rpi import MicrobeamInterfaceRpi

import asyncio, os
import uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

# start pigpiod with 1 µs sample rate before executing this script on RPi with connected hardware:
# sudo pigpiod -s 1
# for testing and when not running on RPi, use simulate=True in MicrobeamInterfaceRpi()
# random hits with Gaussian distribution will be generated in this case

async def main():
    
    logger = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO)
    # start logging to file
    log_formatter = logging.Formatter("%(asctime)s [%(levelname)-5.5s]  %(message)s")
    log_file_handler = logging.FileHandler(os.path.join(os.getcwd(), "beam_control_log.txt"))
    log_file_handler.setFormatter(log_formatter)
    logging.getLogger().addHandler(log_file_handler)


    iface = MicrobeamInterfaceRpi(logger, simulate=True) # simulate=True => testing on a regular computer (no pigpiod)
           
    await iface.init_hw()
    

    run_ctrl = MicrobeamRunController(logger, iface) #, wait_for_client_ack=True)
    
    iface._run_ctrl = run_ctrl  # allows direct access to run_ctrl from interface inside GPIO trigger callback
    
    await run_ctrl.start()

    web_if = MicrobeamWebInterface(logger, run_ctrl)
    await web_if.serve()

loop = asyncio.get_event_loop()
loop.run_until_complete(main())
