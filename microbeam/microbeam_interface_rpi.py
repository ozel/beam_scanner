import asyncio
#import uvloop
#asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
import asyncpio as apio #pip install git+https://github.com/spthm/asyncpio.git
import time
import random
from .microbeam_run_controller import RunState
import numpy as np

class MicrobeamInterfaceRpi:
    def __init__(self, logger, simulate=False):
        self._logger    = logger
        self._simulate = simulate

        self._run_ctrl = None   # will be set in init() of run controller

        self.x =0
        self.y =0
        self.ts =0
        self.i=1
        

        self.trigger = 27 # 5V TTL input, 3-4 us wide positive pulse
        self.ldac=25
        self.shutter = 17 #

        # configure those matching input buffer logic and actual level/pulse polarity
        self.SHUTTER_OPEN = 1 # 1=high or 0=low active? (attention: inverted by output buffer!)
        self.TRIGGER_EDGE = 0 # wait for level going to 0 or 1 ? (attention: inverted by input buffer!)

        # SPI0:  8 = SYNC (CS), 9=MISO (unused), 10=SDIN, 11=SCLK

        self.hits_per_shutter = 1 # number of hits per shutter open event, may be lower than hits_per_step

        # NOTE: if the following delay is too short (< 1 ms) the event_cb() isn't called anymore at some point.
        # this may be realted to a race condition inside the pigpio library.
        self.min_hit_delay = 0.01 # seconds

        self.shutter_closed = True
        self.init_time = None
        self.hits_per_shutter_event = asyncio.Event()
        self.last_tick = 0
        self.event_cb = None

        self.pigpio_script = None
        self.shutters_left = 0

    async def init_hw(self, pigpio_host="raspberrypi.local"):

        self.EDGE_PATTERN = { 1: 0b10, 0: 0b01 } # mask = (new_value | old_value)
        self.DEFAULT_LEVEL = self.TRIGGER_EDGE # always await a real edge transition *after* shutter opens

        self.n_edges_script=f""" 
                        ld  v1 {self.DEFAULT_LEVEL}
                        ld  p9 p0
                        w   p2 {int(self.SHUTTER_OPEN)}
                        dcr p9 
                        tag 1
                            r   p1
                            sta v0
                            rla 1
                            or  v1
                            and 0x3
                            cmp {self.EDGE_PATTERN[self.TRIGGER_EDGE]}
                            ld v1 v0
                            jnz 1
                            dcr p9
                            jp  1
                        w p2 {int(not self.SHUTTER_OPEN)}
                        evt {self.trigger}
        """

        # for only one edge, counting instructions can be skipped
        self.one_edge_script=f""" 
                        ld  v1 {self.DEFAULT_LEVEL}
                        w   p2 {int(self.SHUTTER_OPEN)}
                        tag 1
                            r   p1
                            sta v0
                            rla 1
                            or  v1
                            and 0x3
                            cmp {self.EDGE_PATTERN[self.TRIGGER_EDGE]}
                            ld v1 v0
                            jnz 1
                        w p2 {int(not self.SHUTTER_OPEN)}
                        evt {self.trigger}
        """


        if self._simulate:
            self.init_time = time.time()
        else:
            # init hardware pins
            self.pi = apio.pi()
            await self.pi.connect(pigpio_host)
            await self.pi.set_mode(self.trigger, apio.INPUT)
            await self.pi.set_mode(self.shutter, apio.OUTPUT)
            await self.pi.set_pad_strength(0, 16) # max. 16 mA drive strength on GPIO pin bank 0
            await self.close_shutter()
            #print(pipgio_script)
            #self.pigpio_script = await self.pi.store_script(pipgio_script)

            self.event_cb = await self.pi.event_callback(self.trigger, self._trigger_cb)

            self.spi = await self.pi.spi_open(0,1300000,1)
            await self.pi.set_mode(self.ldac, apio.OUTPUT)
            self._logger.info(f"HW initialized")

    async def prepare_run(self, hits_per_shutter=1):
        if self._simulate is False:
            if hits_per_shutter > self._run_ctrl.hits_per_step:
                self._logger.warning(f"Requested hits_per_shutter ({hits_per_shutter}) > hits_per_step ({self._run_ctrl.hits_per_step}), reduced!")
                hits_per_shutter = self._run_ctrl.hits_per_step
            
            if hits_per_shutter <= 1:
                hits_per_shutter = 1
                self.pigpio_script = await self.pi.store_script(self.one_edge_script)
            else:
                self.pigpio_script = await self.pi.store_script(self.n_edges_script)

            await self.pi.update_script(self.pigpio_script, [hits_per_shutter, self.trigger, self.shutter])
        self.hits_per_shutter = hits_per_shutter
        
    async def simulate_hit(self):
        rand_sleep = random.gauss(mu=0.001, sigma=0.0001)
        self._logger.debug(f"Simulating hit in {rand_sleep:_.03f} s...")
        await asyncio.sleep(rand_sleep)
        if self.shutter_closed is False:
            mu = 10     # grid wire spacing
            sigma = 0.3 # grid wire width
            xmod = np.random.normal(loc=mu, scale=sigma, size=1).astype(int) #round(random.gauss(mu, sigma))
            ymod = np.random.normal(loc=mu, scale=sigma, size=1).astype(int) #round(random.gauss(mu, sigma))
            if bool(np.mod(self.x, xmod) == 0) is not bool(np.mod(self.y, ymod) == 0):
                # simulate regular grid structure
                self._logger.info(f"Simulated hit at x={self.x} % {xmod}, y={self.y} % {ymod}!")
                tick = (time.time() - self.init_time) * 1e6
                await self._trigger_cb(self.trigger, tick)

    async def _trigger_cb(self, event, tick):
        # tick in µs, wraps every 72 minutes
        self.last_tick = tick
        #self._logger.info(f"At least {self.hits_per_shutter} hit(s) seen at time {tick/1000:_.03f} ms")
        self.hits_per_shutter_event.set()          

    async def read_hits(self):  
        self._logger.debug(f"Waiting for hits...")
        await self.hits_per_shutter_event.wait()
        
        #await self.pi.wait_for_event(self.trigger, 60*60) # this only triggers once, why?
        #self.last_tick = time.monotonic_ns()/1000
        
        x = self.x
        y = self.y
        self.hits_per_shutter_event.clear()
        
        self.shutters_left -= 1
        
        if self.shutters_left > 0:
            await self.deliver_hits()

        return self.last_tick, self.hits_per_shutter, x, y
    
    async def deliver_hits(self,hits_per_step=None,enable=True):
        if hits_per_step is not None:
            self.shutters_left = np.ceil(hits_per_step / self.hits_per_shutter)
        if enable:
            hits_delivered = False
            if self._simulate is True:
                await self.simulate_hit()
            else:
                while hits_delivered == False:
                    (s,par) = await self.pi.script_status(self.pigpio_script)
                    if s == 1: # PI_SCRIPT_HALTED
                        await self.pi.run_script(self.pigpio_script)
                        hits_delivered = True
                    elif s == -48: # script already deleted:
                        self._logger.warning(f"Script already deleted!")
                        break
                    else:                    
                        self._logger.debug(f"Script not halted, status {'RUNNING' if (s == 2) else s}, waiting again...")
                        hits_delivered = True                        
                        #FIXME: this is a problem if time timeout occured before
                        await asyncio.sleep(self.min_hit_delay)
                        #await self.pi.run_script(self.pigpio_script)
        else: # cheap shutdown action
            if self.pigpio_script is not None:
                await self.pi.delete_script(self.pigpio_script)
                self.pigpio_script = None

    async def write_dac(self, x, y):
        """Write new X/Y position to DAC"""
        # Glasgow amaranth code:
        #await self._lower.write([x & 0xff, (x >> 8) & 0xff, y & 0xff, (y >> 8) & 0xff])
        #await self._lower.flush()
        if self._simulate is False:
            await self.pi.write(self.ldac,1)
            await self.pi.spi_write(self.spi,[0b00010000, (x >> 8) & 0xff, x & 0xff]) # DAC A = X
            await self.pi.spi_write(self.spi,[0b00010001, (y >> 8) & 0xff, y & 0xff]) # DAC B = Y
            await self.pi.write(self.ldac,0) # latch x and y outputs at the same time
        self.x = x
        self.y = y

    async def close_shutter(self):
        self.shutter_closed = True
        if not self._simulate:
            await self.pi.write(self.shutter,int(not self.SHUTTER_OPEN))
        self._logger.debug(f"Shutter closed")

    async def open_shutter(self):
        if not self._simulate:
            await self.pi.write(self.shutter,int(self.SHUTTER_OPEN))
        self.shutter_closed = False
        self._logger.debug(f"Shutter opened")

    async def set_shutter_override(self, enable):
        # skipped as there is no Glasgow FPGA logic that would control the shutter
        pass

    async def close_hw(self):
        if not self._simulate:
            await self.event_cb.cancel()
            if self.pigpio_script is not None:
                await self.pi.delete_script(self.pigpio_script)
                self.pigpio_script = None
            await self.pi.stop()
            self._logger.info(f"HW closed. In total logged {self._run_ctrl.hit_count} hits.")




