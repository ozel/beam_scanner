"""Run control and interface governance logic"""
import asyncio
#import uvloop
#asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
import enum
import os
import picologging as logging
import time
import json
import aiofiles
import pandas as pd

import numpy as np

class RunState(enum.Enum):
    IDLE = 0
    RUN_ACTIVE = 1

class MicrobeamSubscriberSocket:
    def __init__(self, tcp_server_port=8188):
        self._write_clients = []
        self._read_clients = []
        self.tcp_server_port = tcp_server_port

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

    def __init__(self, logger, iface, wait_for_client_ack=False, fifo_file=None):
        self._logger = logger
        self._iface = iface
        self._iface._run_ctrl = self  # interface class needs direct access to run_ctrl for logging hits from GPIO trigger callback

        self._stop_run_task = None
        self._wait_for_hits_task = None
        self._read_task = None
        self._scan_task = None
        self._scan_run = False

        self.wait_for_client_ack = wait_for_client_ack
        self.swap_xy_in_every_2nd_scan = True # FIXME: add GUI element for this

        self.scan_points = 0
        self.scan_points_done = 0

        self.fifo_file = fifo_file
        
        self.hit_count = 0
        self.hits = []
        #self.step_start_count = 0
        self.hits_per_step = 0 # default
        self.hits_per_step_event = None
        self.timeout_counter = 0

        self.latch_counter = 0
        self.latch_df = None

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

    def _log_hit(self, hw_ts, sys_ts, x, y, hits, latch_up=False):
        # local storage
        if self.state == RunState.RUN_ACTIVE:
            assert self.run_hit_log is not None
            for i in range(hits):
                self.hits.append({'hw_ts': hw_ts, 'sys_ts': sys_ts, 'x': x, 'y': y})
            self.run_hit_log.write(f"{hw_ts},{sys_ts:.7f},{x},{y},{hits},{self.latch_counter if (latch_up is True) else '-'}\n")
            self.run_hit_log.flush()

    async def _read_hit_task(self):
        """FIFO read access / event input queue"""
        sys_ts = 0
        ticks = 0
        x = 0
        y = 0
        hits = 0
        while True:
            ticks, hits, x, y = await self._iface.read_hits() 
            
            sys_ts = time.time()
            self.hit_count += hits
            self._logger.debug(f"At least {hits} hit(s) *logged* at time {ticks/1000:_.03f} ms @ ({x}|{y})")
            self._log_hit(hw_ts=ticks, sys_ts=sys_ts, x=x, y=y, hits=hits,latch_up=self.latch_occured)
            # NOTE: latchup events wil be in most cases logged with the consecutive hit entry! (due to sleep(min_hit_delay) in main loop)

    async def start(self):
        """Launch background tasks controlling event data flow"""
        self.hits_per_step_event = asyncio.Event()
        self._read_task = asyncio.create_task(self._read_hit_task())
        #self._read_task.add_done_callback(self._handle_read_task_result)
        server = await asyncio.start_server(self.subscriber_socket.handle_client, 'localhost', self.subscriber_socket.tcp_server_port)
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
        # poll_period = 0.01  # walue used in 2022 was 10 ms
        if step_timeout == 0:
            step_timeout_count = int(1e9 / self._iface.min_hit_delay) # ~ heat death of universe
        else:
            step_timeout_count = int(step_timeout / self._iface.min_hit_delay)
        
        x_vals = np.linspace(start_x, stop_x, points_x, endpoint=True)
        y_vals = np.linspace(start_y, stop_y, points_y, endpoint=True)

        if units == "volt":
            x_vals = self._dac_voltage_to_lsbs(x_vals)
            y_vals = self._dac_voltage_to_lsbs(y_vals)
        elif units == "um":
            if self._lsb_per_um_x == 0 or self._lsb_per_um_x == 0:
                self._logger.warning("Micrometer unit selected, but system calibration info is invalid, check cal.json file!")
                exit(1)
            x_vals = self._dac_um_to_lsbs_x(x_vals)
            y_vals = self._dac_um_to_lsbs_y(y_vals)
        
        x_vals = x_vals.astype(int)
        y_vals = y_vals.astype(int)

        self.scan_points = len(x_vals) * len(y_vals) * repeat_count
        self.scan_points_done = 0

        self.latch_df = pd.DataFrame()

        if self.wait_for_client_ack is True:
            if not self.subscriber_socket._read_clients:
                self._logger.error("TCP client required for run control, but none connected. Aborting run.")
                return
            
        if self.fifo_file is not None:
            #fifo = await aiofiles.open(self.fifo_file, mode='r')
            fifo = os.open(self.fifo_file, os.O_RDONLY | os.O_NONBLOCK)
            
        await self._iface.prepare_run(hits_per_shutter=1) #FIXME: add GUI element for hits_per_shutter?
            
        step_start_count = 0

       # ensure shutter is closed at start of scan
        await self._iface.close_shutter()
        for repetition in range(repeat_count):
            if self.swap_xy_in_every_2nd_scan is True and (repetition % 2) == 1:
                # first x, then y
                vals_1st_order = x_vals
                vals_2nd_order = y_vals
                self._logger.warning(f"CHANGING SCAN SEQUENCE to first x, then y!")
            else:
                # first y, then x
                vals_1st_order = y_vals
                vals_2nd_order = x_vals
                self._logger.info(f"Default scan sequence: first y, then x.")
            for i in vals_1st_order:
                for j in vals_2nd_order:
                    if self.swap_xy_in_every_2nd_scan is True and (repetition % 2) == 1:
                        x = i
                        y = j
                    else:
                        x = j
                        y = i
                    # new sweep step
                    await self.write_dac(x, y)
                    self._logger.info(f"Scan advancing to point {self.scan_points_done+1} / {self.scan_points}")
                    await self.subscriber_socket.push_msg(f"pos {x} {y}")

                    if self.wait_for_client_ack:
                        wait_for_client_task = asyncio.create_task(self.subscriber_socket.read_ack())
                        #wait_for_tasks.append(wait_for_client_task)
                    
                    step_start_count = self.hit_count

                    timeout_count = 0
                    self.latch_occured = False

                    if self._iface._simulate is True:
                        await self._iface.open_shutter()
                    
                    await self._iface.deliver_hits(hits_per_step)

                    while timeout_count < step_timeout_count and (self.hit_count - step_start_count) < hits_per_step and self._scan_run:
                        if self.wait_for_client_ack is True and wait_for_client_task.done():
                            hits_awaited = (self.hit_count - step_start_count)
                            if hits_awaited >= hits_per_step/2:
                                self._logger.info(f"Step acknowledged by main TCP client, hits per step: {hits_awaited} / {hits_per_step}")
                                # OK, go to next step
                            else:
                                self._logger.info(f"Less than half of hits per step received before TCP client acknowledged, hits per step: {hits_awaited} / {hits_per_step}. Aborting scan!")
                                self._scan_run = False # Abort scan
                                await self.subscriber_socket.push_msg(f"abort")
                                break
                        
                        await asyncio.sleep(self._iface.min_hit_delay)

                        if self.fifo_file is not None:
                            latch_data = None
                            try:
                                #latch_data = await fifo.read()
                                latch_data = os.read(fifo, 1024*1024)
                            except:
                                pass
                                #self._logger.debug(f"No FIFO data available.")
                            if latch_data is not None and len(latch_data) > 0:
                                    self._iface.shutters_left = 0 # prevent future hits at this step, if any
                                    self.latch_occured = True
                                    self.latch_counter += 1
                                    #print(np.frombuffer(latch_data))
                                    latch_data_np = np.frombuffer(latch_data)
                                    new_df = pd.DataFrame( { f"{self.hit_count}_{x}_{y}" : latch_data_np } )
                                    self.latch_df = pd.concat([self.latch_df,new_df], axis=1)
                                    #self.latch_df = self.latch_df.append(new_df, ignore_index=True)
                                    self._logger.info(f"LATCH-UP: {self.latch_counter} logged, hit count: {self.hit_count}. Waiting 5 s to recover.")
                                    #self._logger.debug(f"FIFO data: {len(latch_data)} bytes")
                                    await asyncio.sleep(5) # wait for the latch-up to be over
                                    timeout_count = step_timeout_count # let timeout pass, go to next step

                        timeout_count += 1

                    # -- At this point, either hits_per_step hits were received, timeout reached or scan aborted
                    
                    self._iface.shutters_left = 0

                    if self._iface._simulate is True:
                        await self._iface.close_shutter()

                    if timeout_count == step_timeout_count:
                        self._logger.info(f"Timeout reached ({step_timeout_count } x {self._iface.min_hit_delay}s), moving on.")
                        self.timeout_counter += 1
                    self._logger.debug(f"Step finished, {self.hit_count - step_start_count} hits received.")

                    if not self._scan_run:
                        break
                    else:
                        self.scan_points_done += 1
                if not self._scan_run:
                    break
            if not self._scan_run:
                break
            if self.swap_xy_in_every_2nd_scan is True and repetition == 1:
                open(os.path.join(self.run_dir, f"run_{self.run_id:03d}", "SWAPPED_XY_IN_EVERY_2ND_SCAN_REPETITION"), mode='a').close()
        
        if self.fifo_file is not None:
            os.close(fifo)
            self.latch_df.to_pickle(os.path.join(self.run_dir, f"run_{self.run_id:03d}", "latch_data.pkl"))
            self._logger.info(f"Latch-up counter: {self.latch_counter}")

        self._logger.info(f"Scan finished, {self.scan_points_done} / {self.scan_points} points done.")
        self._logger.info(f"Final hit count: {self.hit_count}, timeouts reached: {self.timeout_counter}.")



        await self.subscriber_socket.push_msg("stop_run")
        # shutter beam before setting beam to origin
        await self._iface.close_shutter() 
        await self.write_dac_voltage(0, 0)
        #await self.subscriber_socket.push_msg(f"pos {x} {y}")
        self.hits_per_step_event.clear()
        await self._iface.deliver_hits(enable=False)

    
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
        
        self._logger.info(f"Run {self.run_id} ended.")

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
        self._logger.info(f"Setting DAC to ({x_lsb}|{y_lsb})")
        await self._iface.write_dac(x_lsb, y_lsb)
        self.dac_x = x_lsb
        self.dac_y = y_lsb

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
        self.run_hit_log.write(f"hw_ts_1us,sys_ts_sec,x_lsb,y_lsb,hits,latch_up\n")

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

        if self._iface._simulate is True:
            self._logger.warning(f"HIT SIMULATION MODE ACTIVATED!")

        self.hits_per_step=hits_per_step
        self.hits_per_step_event.clear()

       # inform subscriber software
        self._logger.info("Informing subscribers...")
        await self.subscriber_socket.push_msg(f"start_run {self.run_id}")

        self.state = RunState.RUN_ACTIVE
        self.hit_count = 0
        self.timeout_counter = 0
        self.latch_counter = 0    
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
        await self._iface.close_shutter()  
        await self.write_dac_voltage(0, 0)  

        """Finishes/aborts any in-progress runs"""
        if self.state == RunState.IDLE:
            self._logger.error("No run to stop!")
            return
        else:
            self._logger.info(f"Stopping run {self.run_id}")
        # complete the current scan
        self._scan_run = False
        await self._scan_task  # wait for scan task to finish
        await self._iface.deliver_hits(enable=False)
      
        assert self.state == RunState.IDLE

