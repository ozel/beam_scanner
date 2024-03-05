import asyncio
#import uvloop
#asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
import asyncpio as apio #pip install git+https://github.com/spthm/asyncpio.git
import time
import random

class MicrobeamInterfaceRpi:
    def __init__(self, logger, simulate=False, external_shutter_control=False):
        self._logger    = logger
        self._simulate = simulate
        self._ext_shutter = external_shutter_control=external_shutter_control

        self._run_ctrl = None   # will be set in init() of run controller

        self.x =0
        self.y =0
        self.ts =0
        self.i=1
        
        # init hardware pins
        self.pi = apio.pi()
        self.trigger = 27 # 5V TTL input, 3-4 us wide positive pulse
        self.ldac=25
        self.shutter = 17 #FIXME: unused so far
        # SPI0:  8 = SYNC (CS), 9=MISO (unused), 10=SDIN, 11=SCLK

        self.shutter_open = False
        self.init_time = None

    async def init_hw(self):
        if self._simulate:
            self.init_time = time.time()
            asyncio.create_task(self.simulate_hits())
        else:
            await self.pi.connect()
            await self.pi.set_mode(self.trigger, apio.INPUT)
            await self.pi.set_mode(self.shutter, apio.OUTPUT)
            await self.close_shutter()
            #await self.pi.set_pull_up_down(self.trigger, apio.PUD_DOWN)
            await self.pi.callback(self.trigger, apio.FALLING_EDGE, self._trigger_cb) #trigger input is inverted!
            self.spi = await self.pi.spi_open(0,1300000,1)
            await self.pi.set_mode(self.ldac, apio.OUTPUT)
        
    async def simulate_hits(self):
            while True:
                await asyncio.sleep(random.gauss(mu=0.0001, sigma=0.000_5))
                tick = (time.time() - self.init_time) * 1000_000
                await self._trigger_cb(self.trigger, 0,tick)

    async def _trigger_cb(self,gpio, level, tick):
        # tick in µs, wraps every 72 minutes

        if self.shutter_open is True or self._ext_shutter:
            # write hit data directly to run controller
            x = self.x
            y = self.y
            sys_ts = time.time()
            self._run_ctrl.hit_count += 1
            self._run_ctrl._log_hit(hw_ts=tick, sys_ts=sys_ts, x=x, y=y)
            self._logger.debug(f"Hit *logged* at time {tick/1000:_.03f} ms @ ({x}|{y})")              
            if self._run_ctrl.hits_per_step is not None:    # FIXME: better: if self._run_ctrl.state == RunState.RUNNING:
                if self._run_ctrl.hit_count - self._run_ctrl.step_start_count >= self._run_ctrl.hits_per_step:
                #    await self.close_shutter() # closing here does not reduce the delay between last hit and shutter closing
                    self._run_ctrl.hits_per_step_event.set()
                # event is cleared and self._run_ctrl.step_start_count is updated by scan_task of run controller
    
        self._logger.debug(f"Hit seen at time {tick/1000:_.03f} ms")
                        
    async def set_led(self, num, value):
        # not implemented
        pass

    async def write_dac(self, x, y):
        """Write new X/Y position to DAC"""
        # Glasgow amaranth code:
        #await self._lower.write([x & 0xff, (x >> 8) & 0xff, y & 0xff, (y >> 8) & 0xff])
        #await self._lower.flush()
        if not self._simulate:
            await self.pi.write(self.ldac,1)
            await self.pi.spi_write(self.spi,[0b00010000, (x >> 8) & 0xff, x & 0xff]) # DAC A = X
            await self.pi.spi_write(self.spi,[0b00010001, (y >> 8) & 0xff, y & 0xff]) # DAC B = Y
            await self.pi.write(self.ldac,0) # latch x and y outputs at the same time
        self.x = x
        self.y = y

    async def close_shutter(self):
        self.shutter_open = False
        if not self._simulate:
            await self.pi.write(self.shutter,1) #FIXME: low/high active?
        self._logger.debug(f"Shutter closed")

    async def open_shutter(self):
        if not self._simulate:
            await self.pi.write(self.shutter,0) #FIXME: low/high active?
        self.shutter_open = True
        self._logger.debug(f"Shutter opened")

    async def set_shutter_override(self, enable):
        # skipped as there is no Glasgow FPGA logic that would control the shutter
        pass



