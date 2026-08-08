import uuid

import websocket

from comfyui_config import comfyui_server

client_id = str(uuid.uuid4())
ws = None
promptID = None
msg_progress = ""
msg_step = ""
current_prompt = None
finished_nodes = []
node_ids = []
last_node = ""
last_time = 0
last_step = 0
cur_node = ""
workflow_start_time = None
