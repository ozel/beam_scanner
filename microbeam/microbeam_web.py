import picologging as logging
import asyncio
#import uvloop
#asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
import aiohttp, aiohttp.web
import os
import json
import socket

class MicrobeamWebInterface:
    def __init__(self, logger, run_ctrl):
        self._logger = logger
        self._run_ctrl = run_ctrl

    def _assemble_html_response(self, content_file):
        html_page = ""
        with open(os.path.join(os.path.dirname(__file__), "frontend", "header.html")) as f:
            html_page += f.read()
        with open(os.path.join(os.path.dirname(__file__), "frontend", content_file)) as f:
            html_page += f.read()
        with open(os.path.join(os.path.dirname(__file__), "frontend", "footer.html")) as f:
            html_page += f.read()
        return html_page

    async def serve_index(self, request):
            index_html = self._assemble_html_response("index.html")
            return aiohttp.web.Response(text=index_html, content_type="text/html")
    
    async def serve_dac(self, request):
            index_html = self._assemble_html_response("dac_control.html")
            return aiohttp.web.Response(text=index_html, content_type="text/html")
    
    async def serve_hit_map(self, request):
            index_html = self._assemble_html_response("hit_map.html")
            return aiohttp.web.Response(text=index_html, content_type="text/html")
    
    async def serve_run_control(self, request):
            index_html = self._assemble_html_response("run_control.html")
            return aiohttp.web.Response(text=index_html, content_type="text/html")

    async def serve_ws(self, request):
        sock = aiohttp.web.WebSocketResponse()
        await sock.prepare(request)

        last_hit_id = 0
        async for msg in sock:
            if msg.type == aiohttp.WSMsgType.TEXT:
                msg_dict = json.loads(msg.data)
                if "action" not in msg_dict:
                    self._logger.error("Invalid WebSocket request (no action provided).")
                if msg_dict["action"] == "write_dac":
                    self._logger.info("Writing DAC position.")
                    assert "units" in msg_dict, "DAC units not provided"
                    assert "dac_x" in msg_dict, "DAC X value not provided in WebSocket Request"
                    assert "dac_y" in msg_dict, "DAC Y value not provided in WebSocket Request"
                    await self._run_ctrl.write_dac_units(
                        x=float(msg_dict["dac_x"]), 
                        y=float(msg_dict["dac_y"]),
                        units=msg_dict["units"],
                    )
                if msg_dict["action"] == "start_run":
                    assert "start_x" in msg_dict, "Start X coordinate not provided"
                    assert "start_y" in msg_dict, "Start Y coordinate not provided"
                    assert "stop_x" in msg_dict, "Stop X coordinate not provided"
                    assert "stop_y" in msg_dict, "Stop Y coordinate not provided"
                    assert "points_x" in msg_dict, "Number of X steps not provided"
                    assert "points_y" in msg_dict, "Number of Y steps not provided"
                    assert "hits_per_step" in msg_dict, "Number of hits per step not provided"
                    assert "repeat_count" in msg_dict, "Number of scan reps not provided"
                    assert "scan_units" in msg_dict, "Scan units not provided"
                    await self._run_ctrl.start_run(
                        start_x=float(msg_dict["start_x"]), 
                        start_y=float(msg_dict["start_y"]),
                        stop_x=float(msg_dict["stop_x"]), 
                        stop_y=float(msg_dict["stop_y"]),
                        points_x=int(msg_dict["points_x"]), 
                        points_y=int(msg_dict["points_y"]),
                        hits_per_step=int(msg_dict["hits_per_step"]),
                        step_timeout=float(msg_dict["step_timeout"]),
                        repeat_count=int(msg_dict["repeat_count"]),
                        units=msg_dict["scan_units"],
                    )
                if msg_dict["action"] == "stop_run":
                    await self._run_ctrl.stop_run()
                if msg_dict["action"] == "poll":
                    if last_hit_id < self._run_ctrl.hit_count:
                        new_hits = [{'x': hit['x'], 'y': hit['y']} for hit in self._run_ctrl.hits[last_hit_id:self._run_ctrl.hit_count]]
                        last_hit_id = self._run_ctrl.hit_count
                        self._logger.info(f"Delivered {len(new_hits)} hits to frontend.")
                    else:
                        new_hits = []

                    response = json.dumps(
                        {
                            "state": self._run_ctrl.state.name,
                            "run_id": self._run_ctrl.run_id,
                            "dac_x": self._run_ctrl.dac_x,
                            "dac_y": self._run_ctrl.dac_y,
                            "scan_points": self._run_ctrl.scan_points,
                            "scan_points_done": self._run_ctrl.scan_points_done,
                            "new_hits": new_hits
                        }
                    )
                    await sock.send_str(response)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                self._logger.error('ws connection closed with exception %s' % ws.exception())

        return sock

    async def serve(self):
        app = aiohttp.web.Application()
        app.add_routes([
            aiohttp.web.get("/",                    self.serve_index),
            aiohttp.web.get("/dac_control.html",    self.serve_dac),
            aiohttp.web.get("/hit_map.html",        self.serve_hit_map),
            aiohttp.web.get("/run_control.html",    self.serve_run_control),
            aiohttp.web.get("/ws",  self.serve_ws),
            aiohttp.web.static("/static", os.path.join(os.path.dirname(__file__), "frontend", "static")),
        ])

        runner = aiohttp.web.AppRunner(app,
            access_log_format='%a(%{X-Forwarded-For}i) "%r" %s "%{Referer}i"')
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, "0.0.0.0", "8088")
        await site.start()
        self._logger.info(f"Web server started. Try: http://{socket.gethostname()}.local:8088/")
        await asyncio.Future()
        
