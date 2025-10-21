import runpod
from runpod.serverless.utils import rp_upload
import os
import websocket
import base64
import json
import uuid
import logging
import urllib.request
import urllib.parse
import binascii
import subprocess
import time
import shutil
import mimetypes

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

server_address = os.getenv('SERVER_ADDRESS', '127.0.0.1')
client_id = str(uuid.uuid4())

# -------------------------
# Helpers de archivos/rutas
# -------------------------

def queue_prompt(prompt):
    url = f"http://{server_address}:8188/prompt"
    logger.info(f"Queueing prompt to: {url}")
    p = {"prompt": prompt, "client_id": client_id}
    data = json.dumps(p).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='ignore')
        logger.error(f"HTTP {e.code} from /prompt. Body:\n{body}")
        raise

def get_image(filename, subfolder, folder_type):
    url = f"http://{server_address}:8188/view"
    logger.info(f"Getting image from: {url}")
    data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
    url_values = urllib.parse.urlencode(data)
    with urllib.request.urlopen(f"{url}?{url_values}") as response:
        return response.read()

def get_history(prompt_id):
    url = f"http://{server_address}:8188/history/{prompt_id}"
    logger.info(f"Getting history from: {url}")
    with urllib.request.urlopen(url) as response:
        return json.loads(response.read())

def get_videos(ws, prompt):
    prompt_id = queue_prompt(prompt)['prompt_id']
    output_videos = {}
    while True:
        out = ws.recv()
        if isinstance(out, str):
            message = json.loads(out)
            if message['type'] == 'executing':
                data = message['data']
                if data['node'] is None and data['prompt_id'] == prompt_id:
                    break
        else:
            continue

    history = get_history(prompt_id)[prompt_id]
    for node_id in history['outputs']:
        node_output = history['outputs'][node_id]
        videos_output = []
        if 'gifs' in node_output:
            for video in node_output['gifs']:
                with open(video['fullpath'], 'rb') as f:
                    video_data = base64.b64encode(f.read()).decode('utf-8')
                videos_output.append(video_data)
        output_videos[node_id] = videos_output

    return output_videos

def load_workflow(workflow_path):
    with open(workflow_path, 'r') as file:
        return json.load(file)

def download_file_from_url(url, output_path):
    try:
        result = subprocess.run(['wget', '-O', output_path, '--no-verbose', url],
                                capture_output=True, text=True)
        if result.returncode == 0:
            logger.info(f"✅ Descargado: {url} -> {output_path}")
            return output_path
        else:
            logger.error(f"❌ wget falló: {result.stderr}")
            raise Exception(f"URL download failed: {result.stderr}")
    except subprocess.TimeoutExpired:
        logger.error("❌ Timeout en descarga")
        raise
    except Exception as e:
        logger.error(f"❌ Error en descarga: {e}")
        raise

def save_base64_to_file(base64_data, temp_dir, output_filename):
    try:
        decoded_data = base64.b64decode(base64_data)
        os.makedirs(temp_dir, exist_ok=True)
        file_path = os.path.abspath(os.path.join(temp_dir, output_filename))
        with open(file_path, 'wb') as f:
            f.write(decoded_data)
        logger.info(f"✅ Base64 guardado en '{file_path}'")
        return file_path
    except (binascii.Error, ValueError) as e:
        logger.error(f"❌ Base64 inválido: {e}")
        raise

def ensure_input_root():
    """ /input debe existir. Intentamos symlink a /runpod-volume/input; si no, creamos carpeta. """
    if not os.path.exists('/input'):
        try:
            os.symlink('/runpod-volume/input', '/input')
            logger.info("Symlink: /input -> /runpod-volume/input")
        except Exception as e:
            os.makedirs('/input', exist_ok=True)
            logger.warning(f"No se pudo symlinkear /input: {e}. Usaré copia.")

def stage_into_input(abs_src_path, subdir):
    """
    Asegura que el archivo esté en /input/<subdir>/<filename>.
    Devuelve (basename, relative_path).
    """
    filename = os.path.basename(abs_src_path)
    dst_dir = os.path.join('/input', subdir) if subdir else '/input'
    os.makedirs(dst_dir, exist_ok=True)
    dst_path = os.path.join(dst_dir, filename)

    if not os.path.exists(dst_path) or os.path.getsize(dst_path) != os.path.getsize(abs_src_path):
        try:
            shutil.copy2(abs_src_path, dst_path)
            logger.info(f"Copiado a /input: {abs_src_path} -> {dst_path}")
        except Exception as e:
            logger.error(f"Error copiando a /input: {e}")
            raise

    logger.info(f"Exists {dst_path}? {os.path.exists(dst_path)} size={os.path.getsize(dst_path)} bytes, mime={mimetypes.guess_type(dst_path)[0]}")
    return filename, f"{subdir}/{filename}" if subdir else filename

def assert_readable(path):
    if not isinstance(path, str):
        raise Exception(f"Path no string: {path}")
    ap = os.path.abspath(path)
    if not os.path.exists(ap):
        raise Exception(f"No existe: {ap}")
    if os.path.isdir(ap):
        raise Exception(f"Es directorio, se esperaba archivo: {ap}")
    if not os.access(ap, os.R_OK):
        raise Exception(f"Sin permiso de lectura: {ap}")
    sz = os.path.getsize(ap)
    if sz == 0:
        raise Exception(f"Archivo vacío: {ap}")
    logger.info(f"OK file: {ap} ({sz} bytes)")
    return ap

def normalize_user_path(p):
    """
    Acepta valores como:
      - '/runpod-volume/input/..'
      - '/input/..'
      - 'images/fixed.jpg' o 'fixed.jpg'
    Devuelve ruta ABSOLUTA existente, de ser posible.
    """
    if not isinstance(p, str):
        return p
    # 1) Si viene como /input/..., apunta al volumen
    if p.startswith('/input/'):
        candidate = p.replace('/input/', '/runpod-volume/input/', 1)
        if os.path.exists(candidate):
            return os.path.abspath(candidate)
    # 2) Si ya es absoluto, devuélvelo
    if p.startswith('/runpod-volume/'):
        return os.path.abspath(p)
    if p.startswith('/'):
        return os.path.abspath(p)
    # 3) Si es relativo (p.ej. images/fixed.jpg o fixed.jpg), prueba dentro del volumen
    for base in ['/runpod-volume/input', '/input']:
        candidate = os.path.join(base, p)
        if os.path.exists(candidate):
            return os.path.abspath(candidate)
    # Como fallback devuelve tal cual (lo validará assert_readable)
    return os.path.abspath(p)

# -------------------------
# Entrada flexible (path/url/base64)
# -------------------------

def process_input(input_data, temp_dir, output_filename, input_type):
    if input_type == "path":
        logger.info(f"📁 path: {input_data}")
        # normaliza paths tipo /input -> /runpod-volume/input, etc.
        return normalize_user_path(input_data)
    elif input_type == "url":
        logger.info(f"🌐 url: {input_data}")
        os.makedirs(temp_dir, exist_ok=True)
        file_path = os.path.abspath(os.path.join(temp_dir, output_filename))
        return download_file_from_url(input_data, file_path)
    elif input_type == "base64":
        logger.info(f"🔢 base64")
        return save_base64_to_file(input_data, temp_dir, output_filename)
    else:
        raise Exception(f"Input type no soportado: {input_type}")

# -------------------------
# Handler principal
# -------------------------

def handler(job):
    job_input = job.get("input", {})
    logger.info(f"Received job input: {job_input}")
    task_id = f"task_{uuid.uuid4()}"

    if job_input.get("image_path") == "/example_image.png":
        return {"video": "test"}

    # Imagen
    if "image_path" in job_input:
        image_path = process_input(job_input["image_path"], task_id, "input_image.jpg", "path")
    elif "image_url" in job_input:
        image_path = process_input(job_input["image_url"], task_id, "input_image.jpg", "url")
    elif "image_base64" in job_input:
        image_path = process_input(job_input["image_base64"], task_id, "input_image.jpg", "base64")
    else:
        image_path = "/examples/image.jpg"
        logger.info("Usando imagen por defecto: /examples/image.jpg")

    # Video
    if "video_path" in job_input:
        video_path = process_input(job_input["video_path"], task_id, "input_video.mp4", "path")
    elif "video_url" in job_input:
        video_path = process_input(job_input["video_url"], task_id, "input_video.mp4", "url")
    elif "video_base64" in job_input:
        video_path = process_input(job_input["video_base64"], task_id, "input_video.mp4", "base64")
    else:
        # Si no hay video, muchos workflows aceptan imagen sola; dejamos imagen por compat.
        video_path = "/examples/image.jpg"
        logger.info("Usando video por defecto (imagen): /examples/image.jpg")

    # Validación física
    image_abs = assert_readable(image_path)
    video_abs = assert_readable(video_path)

    # /input preparado + staging
    ensure_input_root()
    img_basename, img_rel = stage_into_input(image_abs, 'images')   # ('fixed.jpg','images/fixed.jpg')
    vid_basename, vid_rel = stage_into_input(video_abs, 'videos')   # ('fixed.mp4','videos/fixed.mp4')

    # Carga workflow según tengas coordenadas o no
    check_coord = job_input.get("points_store", None)
    if check_coord is None:
        prompt = load_workflow('/newWanAnimate_noSAM_api.json')
    else:
        prompt = load_workflow('/newWanAnimate_point_api.json')

    # IMPORTANTE: asignar NOMBRES (no paths absolutos). Primero intentamos solo basename.
    # Si tu workflow específico requiere subcarpeta, cambia a img_rel / vid_rel.
    prompt["57"]["inputs"]["image"] = img_basename       # o img_rel
    prompt["63"]["inputs"]["video"] = vid_basename       # o vid_rel

    # Resto de params
    prompt["63"]["inputs"]["force_rate"]   = job_input["fps"]
    prompt["30"]["inputs"]["frame_rate"]   = job_input["fps"]
    prompt["65"]["inputs"]["positive_prompt"] = job_input["prompt"]
    prompt["27"]["inputs"]["seed"] = job_input["seed"]
    prompt["27"]["inputs"]["cfg"]  = job_input["cfg"]
    prompt["27"]["inputs"]["steps"] = job_input.get("steps", 4)
    prompt["150"]["inputs"]["value"] = job_input["width"]
    prompt["151"]["inputs"]["value"] = job_input["height"]

    if check_coord is not None:
        prompt["107"]["inputs"]["points_store"]  = job_input["points_store"]
        prompt["107"]["inputs"]["coordinates"]   = job_input["coordinates"]
        prompt["107"]["inputs"]["neg_coordinates"] = job_input["neg_coordinates"]

    logger.info(f"ComfyUI inputs -> image='{prompt['57']['inputs']['image']}', video='{prompt['63']['inputs']['video']}'")

    # Conexión HTTP y WS a ComfyUI
    ws_url = f"ws://{server_address}:8188/ws?clientId={client_id}"
    http_url = f"http://{server_address}:8188/"
    logger.info(f"Checking HTTP connection to: {http_url}")

    max_http_attempts = 180
    for http_attempt in range(max_http_attempts):
        try:
            urllib.request.urlopen(http_url, timeout=5)
            logger.info(f"HTTP OK (try {http_attempt+1})")
            break
        except Exception as e:
            logger.warning(f"HTTP FAIL (try {http_attempt+1}/{max_http_attempts}): {e}")
            if http_attempt == max_http_attempts - 1:
                raise Exception("No se puede conectar a ComfyUI (HTTP).")
            time.sleep(1)

    ws = websocket.WebSocket()
    max_attempts = int(180/5)
    for attempt in range(max_attempts):
        try:
            ws.connect(ws_url)
            logger.info(f"WS OK (try {attempt+1})")
            break
        except Exception as e:
            logger.warning(f"WS FAIL (try {attempt+1}/{max_attempts}): {e}")
            if attempt == max_attempts - 1:
                raise Exception("Timeout de WebSocket (3 min)")
            time.sleep(5)

    videos = get_videos(ws, prompt)
    ws.close()

    for node_id in videos:
        if videos[node_id]:
            return {"video": videos[node_id][0]}

    return {"error": "No se encontró video en la salida."}

runpod.serverless.start({"handler": handler})
