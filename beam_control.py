#!/usr/bin/env python3
import picologging as logging
from microbeam.microbeam_web import MicrobeamWebInterface
from microbeam.microbeam_run_controller import MicrobeamRunController
from microbeam.microbeam_interface_rpi import MicrobeamInterfaceRpi

import asyncio, os, signal
import uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

# quick notes: (FIXME: create README.md)
# 1. run this script with Python 3.9 or higher
# 2. important dependencies: asyncpio, aiohttp, uvloop, pip install git+https://github.com/spthm/asyncpio.git
# 3. there's a simulation mode (see below), a RPi with GPIOs is not mandatory (but async_p_io must be still installed!)

# for GPIO access on a Raspberry Pi, pigpiod must be running with 1 µs sample rate:
# sudo pigpiod -s 1
# for testing or when not running on RPi, use simulate=True in MicrobeamInterfaceRpi()
# random hits with Gaussian distribution will be generated in this case

# multiple TCP clients may connect to port 8188 and get run_start & run id, run_stop and x/y position messages
# optionally, if wait_for_client_ack=True is set (see below), the *main* TCP client (=first one that was connected) 
# must reply with any message & new line (e.g. "ack\n") before advancing the ion beam to the next step
# => read all registers of the ASIC and do what you need to check for a SEU, then send "ack\n" to the server

# example nc session:
# $ nc localhost 8188
# start_run 11
# pos -99 -93
# ack (<= user input)
# pos -50 -93
# ack
# pos 0 -93
# ack
# pos 50 -93
# ack
# pos 99 -93
# ack
# stop_run

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG) # use .DEBUG only for testing, it will destroy timing!
# start logging to file
#log_formatter = logging.Formatter("%(asctime)s [%(levelname)s]  %(message)s") # bug in picologging with asctime
log_formatter = logging.Formatter("%(created)f [%(levelname)s]  %(message)s")
log_file_handler = logging.FileHandler(os.path.join(os.getcwd(), "beam_control_log.txt"))
log_file_handler.setFormatter(log_formatter)
logging.getLogger().addHandler(log_file_handler)

iface = MicrobeamInterfaceRpi(logger,simulate=False) # simulate=True => testing on a regular computer (no pigpiod)
    
run_ctrl = MicrobeamRunController(logger, iface, wait_for_client_ack = False, fifo_file='/tmp/latch_fifo') # if True, the main TCP client must reply with a new line character (any message) before advancing the ion beam to the next step

async def main():

    await iface.init_hw(pigpio_host="192.168.0.200")
    
    await run_ctrl.start()

    logger.info(f"Listening to TCP clients on port {run_ctrl.subscriber_socket.tcp_server_port}.")

    web_if = MicrobeamWebInterface(logger, run_ctrl)
    await web_if.serve()

    await iface.close_hw()

try:
    asyncio.run(main())
except KeyboardInterrupt:
    logger.info("KeyboardInterrupt received, stopping run controller and closing hardware interface.")
    asyncio.run(run_ctrl.stop_run())
    asyncio.run(iface.close_hw())
    logger.info("Exiting.")

