"""Run control and interface governance logic"""
import asyncio
import uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
import enum
import os
import logging
import time
import json

import numpy as np

class RunState(enum.Enum):
    IDLE = 0
    RUN_ACTIVE = 1

class MicrobeamSubscriberSocket:
    def __init__(self):
        self._write_clients = []
        self._read_clients = []

    async def handle_client(self, reader, writer):
        logging.info("Adding TCP subscriber to client list")
        self._read_clients.append(reader)
        self._write_clients.append(writer)

    
    async def push_msg(self, msg):
        removal_queue = []

        for writer in self._write_clients:
            try:
                writer.write((msg + "\n").encode('utf8'))
                await writer.drain()
            except ConnectionResetError:
                removal_queue.append(writer)

        for dead_writer in removal_queue:
            logging.info("Removing TCP subscriber from client list")
            dead_writer.close()
            self._write_clients.pop(dead_writer)

    async def read_ack(self):
        if self._read_clients:
            data = await self._read_clients[0].readline()
            if data.decode('utf8').rstrip() == "ack":
                return True
            return False

class MicrobeamRunController:
    """Run control and bookkeeping class"""

    def __init__(self, logger, iface, wait_for_client_ack=False):
        self._logger = logger
        self._iface = iface
        self._read_task = None
        self._scan_task = None
        self._scan_run = False

        self.wait_for_client_ack = wait_for_client_ack
        self.scan_points = 0
        self.scan_points_done = 0

        self.hit_count = 0
        self.hits = []

        self.dac_x = 0
        self.dac_y = 0
        self.state = RunState.IDLE

        self.run_dir = os.getcwd()

        self.run_log_handler = None
        self.run_hit_log = None

        self.subscriber_socket = MicrobeamSubscriberSocket()

        if not os.path.exists(os.path.join(self.run_dir, "run.id")):
            with open(os.path.join(self.run_dir, "run.id"), "w") as fd:
                fd.write(str(-1))
            self.run_id = -1
        else:
            with open(os.path.join(self.run_dir, "run.id"), "r") as fd:
                self.run_id = int(fd.read())

        if os.path.exists(os.path.join(self.run_dir, "cal.json")):
            with open(os.path.join(self.run_dir, "cal.json"), "r") as fd:
                cal_data = json.load(fd)
            assert "lsb_per_um_x" in cal_data, "X calibration data invalid"
            assert "lsb_per_um_y" in cal_data, "Y calibration data invalid"
            self._lsb_per_um_x = cal_data["lsb_per_um_x"]
            self._lsb_per_um_y = cal_data["lsb_per_um_y"]
            self._logger.info("Calibration data loaded.")

        else:
            self._logger.warning("System micrometer scale is not calibrated!")
            self._lsb_per_um_x = 0
            self._lsb_per_um_y = 0
    
    def _dac_um_to_lsbs_x(self, x_um):
        return np.clip(np.round(x_um * self._lsb_per_um_x), -32768, 32767)
    
    def _dac_um_to_lsbs_y(self, y_um):
        return np.clip(np.round(y_um * self._lsb_per_um_y), -32768, 32767)
    
    def _dac_voltage_to_lsbs(self, voltage):
        return np.clip(np.round(voltage / 10.0 * 32768.0), -32768, 32767)

    def _log_hit(self, hw_ts, sys_ts, x, y):
        # local storage
        if self.state == RunState.RUN_ACTIVE:
            assert self.run_hit_log is not None
            self.hits.append({'hw_ts': hw_ts, 'sys_ts': sys_ts, 'x': x, 'y': y})
            self.run_hit_log.write(f"{hw_ts},{sys_ts},{x},{y}\n")
            self.run_hit_log.flush()

    async def _read_hit_task(self):
        """FIFO read access / event input queue"""
        while True:
            hw_ts, x, y = await self._iface.read_hit()  # blocks until new hit is available
            sys_ts = time.time()
            self.hit_count += 1
            self._logger.info(f"Hit at time {hw_ts/1000:_.03f} ms @ ({x}|{y})")
            self._log_hit(hw_ts=hw_ts, sys_ts=sys_ts, x=x, y=y)

    async def start(self):
        """Launch background tasks controlling event data flow"""
        self._read_task = asyncio.create_task(self._read_hit_task())
        self._read_task.add_done_callback(self._handle_read_task_result)
        server = await asyncio.start_server(self.subscriber_socket.handle_client, 'localhost', 8188)
        asyncio.create_task(server.serve_forever())
        
    async def _scan_generator_task(
            self,
            start_x,
            start_y,
            stop_x,
            stop_y,
            points_x,
            points_y,
            hits_per_step,
            step_timeout,
            repeat_count,
            units,
        ):
        """Scan generation logic"""
        self._scan_run = True  # external scan abort signal

        # poll period
        poll_period = 0.01
        if step_timeout == 0:
            step_timeout_count = int(1e9 / poll_period) # ~ heat death of universe
        else:
            step_timeout_count = int(step_timeout / poll_period)
        
        x_vals = np.linspace(start_x, stop_x, points_x, endpoint=True)
        y_vals = np.linspace(start_y, stop_y, points_y, endpoint=True)

        if units == "volt":
            x_vals = self._dac_voltage_to_lsbs(x_vals)
            y_vals = self._dac_voltage_to_lsbs(y_vals)
        if units == "um":
            if self._dac_um_to_lsbs_x == 0 or self._dac_um_to_lsbs_y == 0:
                self._logger.warning("System calibration info is invalid!")
            x_vals = self._dac_um_to_lsbs_x(x_vals)
            y_vals = self._dac_um_to_lsbs_y(y_vals)
        
        x_vals = x_vals.astype(int)
        y_vals = y_vals.astype(int)

        self.scan_points = len(x_vals) * len(y_vals) * repeat_count
        self.scan_points_done = 0

        # clear shutter override during scan
        await self._iface.set_shutter_override(False)
        if self.wait_for_client_ack is True:
            if not self.subscriber_socket._read_clients:
                self._logger.error("TCP client required for run control, but none connected. Aborting run.")
                return

        # ensure shutter is closed at start of scan
        await self._iface.close_shutter()
        for _ in range(repeat_count):
            for y in y_vals:
                for x in x_vals:
                    # new sweep step
                    start_count = self.hit_count
                    timeout_count = 0
                    self._logger.info(f"Scan advancing to point {self.scan_points_done+1} / {self.scan_points}")
                    await self.write_dac(x, y)
                    await self._iface.open_shutter()
                    self._logger.debug(f"Shutter open")
                    await self.subscriber_socket.push_msg(f"pos {x} {y}")
                    while timeout_count < step_timeout_count and self.hit_count - start_count < hits_per_step and self._scan_run:
                        await asyncio.sleep(poll_period)
                        timeout_count += 1
                    await self._iface.close_shutter()
                    self._logger.debug(f"Shutter closed")
                    self.scan_points_done += 1
                    if not self._scan_run:
                        break
                if not self._scan_run:
                    break
            if not self._scan_run:
                break
        
        self._logger.info(f"Scan finished, {self.scan_points_done} / {self.scan_points} points done.")
        await self.subscriber_socket.push_msg("stop_run")
        # shutter beam before setting beam to origin
        await self._iface.close_shutter() 
        await self.write_dac_voltage(0, 0)
    
    def _handle_read_task_result(self, task):
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            self._logger.exception('Exception raised by read task = %r', task)

    def _handle_scan_task_result(self, task):
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            self._logger.exception('Exception raised by scan task = %r', task)
        
        self._logger.info("Run ended.")

        # close all files
        self.run_hit_log.close()
        self.run_hit_log = None

        # remove run-specific log handler
        logging.getLogger().removeHandler(self.run_log_handler)
        self.run_log_handler = None

        # reset internal run state 
        self.state = RunState.IDLE

    async def write_dac(self, x_lsb, y_lsb):
        """Base function for DAC access (takes care of position housekeeping)"""
        x_lsb = int(x_lsb)
        y_lsb = int(y_lsb)
        assert -32768 <= x_lsb <= 32767
        assert -32768 <= y_lsb <= 32767
        self.dac_x = x_lsb
        self.dac_y = y_lsb
        self._logger.info(f"Setting DAC to ({x_lsb}|{y_lsb})")
        await self._iface.write_dac(x_lsb, y_lsb)

    async def write_dac_voltage(self, x, y):
        assert -10 <= x <= 10
        assert -10 <= y <= 10
        await self.write_dac(self._dac_voltage_to_lsbs(x), self._dac_voltage_to_lsbs(y))
    
    async def write_dac_units(self, x, y, units):
        assert units in ["lsb", "um", "volt"], "Invalid unit provided to write_dac_units"

        if units == "lsb":
            await self.write_dac(x, y)
        if units == "volt":
            await self.write_dac(self._dac_voltage_to_lsbs(x), self._dac_voltage_to_lsbs(y))
        if units == "um":
            await self.write_dac(self._dac_um_to_lsbs_x(x), self._dac_um_to_lsbs_y(y))

    async def start_run(
            self,
            start_x,
            start_y,
            stop_x,
            stop_y,
            points_x,
            points_y,
            hits_per_step,
            step_timeout,
            repeat_count,
            units,
        ):
        """Starts a new run with set of parameters provided by front-end"""

        assert units in ["um", "lsb", "volt"], "Invalid unit supplied for tun"

        if self.state != RunState.IDLE:
            self._logger.error("Not starting a new run (system state not IDLE)")
            return

        self.run_id += 1
        self._logger.info(f"Starting new run {self.run_id}")
        with open(os.path.join(self.run_dir, "run.id"), "w") as fd:
            fd.write(str(self.run_id))
        os.mkdir(os.path.join(self.run_dir, f"run_{self.run_id:03d}"))
        
        # set up run logging
        logFormatter = logging.Formatter("%(asctime)s [%(levelname)-5.5s]  %(message)s")
        self.run_log_handler = logging.FileHandler(os.path.join(self.run_dir, f"run_{self.run_id:03d}", "run_log.txt"))
        self.run_log_handler.setFormatter(logFormatter)
        logging.getLogger().addHandler(self.run_log_handler)

        # set up hit log
        self.run_hit_log = open(os.path.join(self.run_dir, f"run_{self.run_id:03d}", "hit_log.csv"), "w")
        self.run_hit_log.write(f"hw_ts_10us,sys_ts_sec,x_lsb,y_lsb\n")

        self._logger.info(f"Start of run {self.run_id}")
        self._logger.info(f"Run parameters:")
        self._logger.info(f"Scan unit: {units}")
        self._logger.info(f"X Start: {start_x}, X Stop: {stop_x}, X Points: {points_x}")
        self._logger.info(f"Y Start: {start_y}, Y Stop: {stop_y}, Y Points: {points_y}")
        self._logger.info(f"Hits per step: {hits_per_step}, Step timeout: {step_timeout}, Repeat count: {repeat_count}")
        self._logger.info(f"")
        self._logger.info(f"Calibration Coefficients")
        self._logger.info(f"X scale: {self._lsb_per_um_x} LSB/micrometer")
        self._logger.info(f"Y scale: {self._lsb_per_um_y} LSB/micrometer")

        # inform subscriber software
        self._logger.info("Informing subscribers...")
        await self.subscriber_socket.push_msg(f"start_run {self.run_id}")

        self.state = RunState.RUN_ACTIVE
        self.hit_count = 0
        self.hits = []
        
        self._scan_task = asyncio.create_task(
            self._scan_generator_task(
                start_x=start_x,
                start_y=start_y,
                stop_x=stop_x,
                stop_y=stop_y,
                points_x=points_x,
                points_y=points_y,
                hits_per_step=hits_per_step,
                step_timeout=step_timeout,
                repeat_count=repeat_count,
                units=units
            )
        )
        self._scan_task.add_done_callback(self._handle_scan_task_result)

    async def stop_run(self):
        """Finishes/aborts any in-progress runs"""
        if self.state == RunState.IDLE:
            self._logger.error("No run to stop!")
            return
        # complete the current scan
        self._scan_run = False
        await self._scan_task  # wait for scan task to finish
        #await self._iface.close_shutter(True)  # already handled by scan task
        #await self.write_dac_voltage(0, 0)            # already handled by scan task
        assert self.state == RunState.IDLE

