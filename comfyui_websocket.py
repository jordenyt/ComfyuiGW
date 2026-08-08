import json
import time
from threading import Thread

import websocket

import comfyui_state as st
from comfyui_config import comfyui_server
from comfyui_state import client_id


def format_time(seconds):
    """Convert seconds to mm:ss format."""
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"


def open_websocket_connection():
    st.ws = websocket.WebSocketApp(
        f"ws://{comfyui_server}/ws?clientId={client_id}",
        on_message=ws_message,
    )
    Thread(target=st.ws.run_forever).start()
    return st.ws


def ws_message(_ws, msg):
    def run():
        try:
            if isinstance(msg, bytes):
                try:
                    decoded_msg = msg.decode('utf-8')
                except UnicodeDecodeError:
                    return
            else:
                decoded_msg = msg

            message = json.loads(decoded_msg)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        if message['type'] == 'progress':
            data = message['data']
            current_step = data['value']
            if current_step == data['max']:
                st.msg_step = ""
            elif st.last_node == st.cur_node:
                elapsed_time = time.time() - st.last_time
                avg_time_per_step = elapsed_time
                if current_step > st.last_step:
                    avg_time_per_step = elapsed_time / (current_step - st.last_step)
                elif current_step > 0:
                    avg_time_per_step = elapsed_time / current_step
                remaining_steps = data['max'] - current_step
                estimated_time = remaining_steps * avg_time_per_step
                st.msg_step = f"--> Step: {current_step}/{data['max']} [{format_time(avg_time_per_step * current_step)}<{format_time(estimated_time)}]"
            else:
                st.last_node = st.cur_node
                st.last_time = time.time()
                st.last_step = current_step
                st.msg_step = f"--> Step: {current_step}/{data['max']}"

        elif message['type'] == 'execution_cached':
            data = message['data']
            for itm in data['nodes']:
                if itm not in st.finished_nodes:
                    st.finished_nodes.append(itm)
                    st.msg_progress = f"{len(st.finished_nodes)} of {len(st.node_ids)} nodes processed."

        elif message['type'] == 'executing':
            data = message['data']
            if data.get('prompt_id') != st.promptID:
                return
            if data['node'] is None:
                st.msg_step = ""
                st.msg_progress = "Done"
            elif data['node'] not in st.finished_nodes:
                st.finished_nodes.append(data['node'])
                st.cur_node = data['node']
                node_info = st.current_prompt.get(data['node'], {}) if st.current_prompt else {}
                title = node_info.get('_meta', {}).get('title')
                label = title if title else data['node']
                st.msg_progress = f"Processing \"{label}\" ({len(st.finished_nodes)}/{len(st.node_ids)})..."

        elif message['type'] == 'execution_interrupted':
            data = message['data']
            if data['prompt_id'] == st.promptID:
                st.msg_step = ""
                st.msg_progress = "Done"

    Thread(target=run).start()
