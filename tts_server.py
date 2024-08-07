import json
import time
import os
from pathlib import Path
import torch
import torchaudio
from TTS.tts.configs.xtts_config import XttsConfig
from TTS.tts.models.xtts import Xtts
import io
import wave
from pydub import AudioSegment
import librosa
import pyrubberband
import soundfile as sf
from datetime import datetime
import random
import string
import re
import whisper
import asyncio
import pyrubberband

##########################
#### Webserver Imports####
##########################
from fastapi import (
    FastAPI,
    Form,
    Request,
    Response,
    Depends,
    HTTPException,
    File,
    UploadFile
)
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from urllib.parse import urlparse
import requests
from urllib.parse import unquote
import httpx
import shutil

###########################
#### STARTUP VARIABLES ####
###########################
# STARTUP VARIABLE - Create "this_dir" variable as the current script directory
this_dir = Path(__file__).parent.resolve()
# STARTUP VARIABLE - Set "device" to cuda if exists, otherwise cpu
device = "cuda" if torch.cuda.is_available() else "cpu"
# STARTUP VARIABLE - Import languges file for Gradio to be able to display them in the interface
with open(this_dir / "languages.json", encoding="utf8") as f:
    languages = json.load(f)
# Base setting for a possible FineTuned model existing and the loader being available
tts_method_xtts_ft = False

# information_file_path 为包含角色信息的文件地址，需要修改
information_file_path = "/root/autodl-tmp/jinbao/TTS/data.json"

# 语音转文字的模型
STT_model = whisper.load_model("tiny.en")

#################################################################
#### LOAD PARAMS FROM confignew.json - REQUIRED FOR BRANDING ####
#################################################################
# Load config file and get settings
def load_config(file_path):
    with open(file_path, "r") as configfile_path:
        configfile_data = json.load(configfile_path)
    return configfile_data


# Define the path to the confignew.json file
configfile_path = this_dir / "confignew.json"

# Load confignew.json and assign it to a different variable (config_data)
params = load_config(configfile_path)
# check someone hasnt enabled lowvram on a system thats not cuda enabled
params["low_vram"] = "false" if not torch.cuda.is_available() else params["low_vram"]

# Load values for temperature and repetition_penalty
temperature = params["local_temperature"]
repetition_penalty = params["local_repetition_penalty"]

# Define the path to the JSON file
config_file_path = this_dir / "modeldownload.json"

#############################################
#### LOAD PARAMS FROM MODELDOWNLOAD.JSON ####
############################################
# This is used only in the instance that someone has changed their model path
# Define the path to the JSON file
modeldownload_config_file_path = this_dir / "modeldownload.json"

# Check if the JSON file exists
if modeldownload_config_file_path.exists():
    with open(modeldownload_config_file_path, "r") as modeldownload_config_file:
        modeldownload_settings = json.load(modeldownload_config_file)

    # Extract settings from the loaded JSON
    modeldownload_base_path = Path(modeldownload_settings.get("base_path", ""))
    modeldownload_model_path = Path(modeldownload_settings.get("model_path", ""))
else:
    # Default settings if the JSON file doesn't exist or is empty
    print(
        f"[{params['branding']}Startup] \033[91mWarning\033[0m modeldownload.config is missing so please re-download it and save it in the alltalk_tts main folder."
    )

##################################################
#### Check to see if a finetuned model exists ####
##################################################
# Set the path to the directory
trained_model_directory = this_dir / "models" / "trainedmodel"
# Check if the directory "trainedmodel" exists
finetuned_model = trained_model_directory.exists()
# If the directory exists, check for the existence of the required files
if finetuned_model:
    required_files = ["model.pth", "config.json", "vocab.json"]
    finetuned_model = all((trained_model_directory / file).exists() for file in required_files)

########################
#### STARTUP CHECKS ####
########################
try:
    from TTS.api import TTS
    from TTS.utils.synthesizer import Synthesizer
except ModuleNotFoundError:
    print(
        f"[{params['branding']}Startup] \033[91mWarning\033[0m Could not find the TTS module. Make sure to install the requirements for the alltalk_tts extension.",
        f"[{params['branding']}Startup] \033[91mWarning\033[0m Linux / Mac:\npip install -r extensions/alltalk_tts/requirements.txt\n",
        f"[{params['branding']}Startup] \033[91mWarning\033[0m Windows:\npip install -r extensions\\alltalk_tts\\requirements.txt\n",
        f"[{params['branding']}Startup] \033[91mWarning\033[0m If you used the one-click installer, paste the command above in the terminal window launched after running the cmd_ script. On Windows, that's cmd_windows.bat."
    )
    raise

# DEEPSPEED Import - Check for DeepSpeed and import it if it exists
deepspeed_available = False
try:
    import deepspeed
    deepspeed_available = True
except ImportError:
    pass
if deepspeed_available:
    print(f"[{params['branding']}Startup] DeepSpeed \033[93mDetected\033[0m")
    print(f"[{params['branding']}Startup] Activate DeepSpeed in {params['branding']}settings")
else:
    print(f"[{params['branding']}Startup] DeepSpeed \033[93mNot Detected\033[0m. See https://github.com/microsoft/DeepSpeed")


@asynccontextmanager
async def startup_shutdown(no_actual_value_it_demanded_something_be_here):
    await setup()
    yield
    # Shutdown logic


# Create FastAPI app with lifespan
app = FastAPI(lifespan=startup_shutdown)
# Allow all origins, and set other CORS options
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Set this to the specific origins you want to allow
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

#####################################
#### MODEL LOADING AND UNLOADING ####
#####################################
# MODEL LOADERS Picker For API TTS, API Local, XTTSv2 Local, XTTSv2 FT
async def setup():
    global device
    # Set a timer to calculate load times
    generate_start_time = time.time()  # Record the start time of loading the model
    # Start loading the correct model as set by "tts_method_api_tts", "tts_method_api_local" or "tts_method_xtts_local" being True/False
    if params["tts_method_api_tts"]:
        print(
            f"[{params['branding']}Model] \033[94mAPI TTS Loading\033[0m {params['tts_model_name']} \033[94minto\033[93m",
            device,
            "\033[0m",
        )
        model = await api_load_model()
    elif params["tts_method_api_local"]:
        print(
            f"[{params['branding']}Model] \033[94mAPI Local Loading\033[0m {modeldownload_model_path} \033[94minto\033[93m",
            device,
            "\033[0m",
        )
        model = await api_manual_load_model()
    elif params["tts_method_xtts_local"]:
        print(
            f"[{params['branding']}Model] \033[94mXTTSv2 Local Loading\033[0m {modeldownload_model_path} \033[94minto\033[93m",
            device,
            "\033[0m",
        )
        model = await xtts_manual_load_model()
    elif tts_method_xtts_ft:
        print(
            f"[{params['branding']}Model] \033[94mXTTSv2 FT Loading\033[0m /models/fintuned/model.pth \033[94minto\033[93m",
            device,
            "\033[0m",
        )
        model = await xtts_ft_manual_load_model()
    # Create an end timer for calculating load times
    generate_end_time = time.time()
    # Calculate start time minus end time
    generate_elapsed_time = generate_end_time - generate_start_time
    # Print out the result of the load time
    print(
        f"[{params['branding']}Model] \033[94mModel Loaded in \033[93m{generate_elapsed_time:.2f} seconds.\033[0m"
    )
    # Set "tts_model_loaded" to true
    params["tts_model_loaded"] = True
    # Set the output path for wav files
    output_directory = this_dir / params["output_folder_wav_standalone"]
    output_directory.mkdir(parents=True, exist_ok=True)
    #Path(f'this_folder/outputs/').mkdir(parents=True, exist_ok=True)


# MODEL LOADER For "API TTS"
async def api_load_model():
    global model
    model = TTS(params["tts_model_name"]).to(device)
    return model


# MODEL LOADER For "API Local"
async def api_manual_load_model():
    global model
    # check to see if a custom path has been set in modeldownload.json and use that path to load the model if so
    if str(modeldownload_base_path) == "models":
        model = TTS(
            model_path=this_dir / "models" / modeldownload_model_path,
            config_path=this_dir / "models" / modeldownload_model_path / "config.json",
        ).to(device)
    else:
        print(
            f"[{params['branding']}Model] \033[94mInfo\033[0m Loading your custom model set in \033[93mmodeldownload.json\033[0m:",
            modeldownload_base_path / modeldownload_model_path,
        )
        model = TTS(
            model_path=modeldownload_base_path / modeldownload_model_path,
            config_path=modeldownload_base_path / modeldownload_model_path / "config.json",
        ).to(device)
    return model


# MODEL LOADER For "XTTSv2 Local"
async def xtts_manual_load_model():
    global model
    config = XttsConfig()
    # check to see if a custom path has been set in modeldownload.json and use that path to load the model if so
    if str(modeldownload_base_path) == "models":
        config_path = this_dir / "models" / modeldownload_model_path / "config.json"
        vocab_path_dir = this_dir / "models" / modeldownload_model_path / "vocab.json"
        checkpoint_dir = this_dir / "models" / modeldownload_model_path
    else:
        print(
            f"[{params['branding']}Model] \033[94mInfo\033[0m Loading your custom model set in \033[93mmodeldownload.json\033[0m:",
            modeldownload_base_path / modeldownload_model_path,
        )
        config_path = modeldownload_base_path / modeldownload_model_path / "config.json"
        vocab_path_dir = modeldownload_base_path / modeldownload_model_path / "vocab.json"
        checkpoint_dir = modeldownload_base_path / modeldownload_model_path
    config.load_json(str(config_path))
    model = Xtts.init_from_config(config)
    model.load_checkpoint(
        config,
        checkpoint_dir=str(checkpoint_dir),
        vocab_path=str(vocab_path_dir),
        use_deepspeed=params["deepspeed_activate"],
    )
    model.to(device)
    return model

# MODEL LOADER For "XTTSv2 FT"
async def xtts_ft_manual_load_model():
    global model
    config = XttsConfig()
    config_path = this_dir / "models" / "trainedmodel" / "config.json"
    vocab_path_dir = this_dir / "models" / "trainedmodel" / "vocab.json"
    checkpoint_dir = this_dir / "models" / "trainedmodel"
    config.load_json(str(config_path))
    model = Xtts.init_from_config(config)
    model.load_checkpoint(
        config,
        checkpoint_dir=str(checkpoint_dir),
        vocab_path=str(vocab_path_dir),
        use_deepspeed=params["deepspeed_activate"],
    )
    model.to(device)
    return model

# MODEL UNLOADER
async def unload_model(model):
    print(f"[{params['branding']}Model] \033[94mUnloading model \033[0m")
    del model
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    params["tts_model_loaded"] = False
    return None


# MODEL - Swap model based on Gradio selection API TTS, API Local, XTTSv2 Local
async def handle_tts_method_change(tts_method):
    global model
    global tts_method_xtts_ft
    # Update the params dictionary based on the selected radio button
    print(
        f"[{params['branding']}Model] \033[94mChanging model \033[92m(Please wait 15 seconds)\033[0m"
    )
    # Set other parameters to False
    if tts_method == "API TTS":
        params["tts_method_api_local"] = False
        params["tts_method_xtts_local"] = False
        params["tts_method_api_tts"] = True
        params["deepspeed_activate"] = False
        tts_method_xtts_ft = False
    elif tts_method == "API Local":
        params["tts_method_api_tts"] = False
        params["tts_method_xtts_local"] = False
        params["tts_method_api_local"] = True
        params["deepspeed_activate"] = False
        tts_method_xtts_ft = False
    elif tts_method == "XTTSv2 Local":
        params["tts_method_api_tts"] = False
        params["tts_method_api_local"] = False
        params["tts_method_xtts_local"] = True
        tts_method_xtts_ft = False
    elif tts_method == "XTTSv2 FT":
        tts_method_xtts_ft = True
        params["tts_method_api_tts"] = False
        params["tts_method_api_local"] = False
        params["tts_method_xtts_local"] = False

    # Unload the current model
    model = await unload_model(model)

    # Load the correct model based on the updated params
    await setup()


def check_or_download_voice(voice_url: string):
    local_voice_dir = "./voices/"
    filename = os.path.basename(urlparse(voice_url).path)
    local_voice_path = os.path.join(local_voice_dir, filename)
    
    if os.path.exists(local_voice_path):
        return os.path.abspath(local_voice_path)
    else:
        print(f"Downloading voice file from {voice_url}...")
        os.makedirs(local_voice_dir, exist_ok=True)
        response = requests.get(voice_url)
        with open(local_voice_path, "wb") as f:
            f.write(response.content)
        print(f"Voice file downloaded and saved to {local_voice_path}")
        return os.path.abspath(local_voice_path)



# MODEL WEBSERVER- API Swap Between Models
@app.route("/api/reload", methods=["POST"])
async def reload(request: Request):
    tts_method = request.query_params.get("tts_method")
    if tts_method not in ["API TTS", "API Local", "XTTSv2 Local", "XTTSv2 FT"]:
        return {"status": "error", "message": "Invalid TTS method specified"}
    await handle_tts_method_change(tts_method)
    return Response(
        content=json.dumps({"status": "model-success"}), media_type="application/json"
    )


##################
#### LOW VRAM ####
##################
# LOW VRAM - MODEL MOVER VRAM(cuda)<>System RAM(cpu) for Low VRAM setting
async def switch_device():
    global model, device
    # Check if CUDA is available before performing GPU-related operations
    if torch.cuda.is_available():
        if device == "cuda":
            device = "cpu"
            model.to(device)
            torch.cuda.empty_cache()
        else:
            device == "cpu"
            device = "cuda"
            model.to(device)


@app.post("/api/lowvramsetting")
async def set_low_vram(request: Request, new_low_vram_value: bool):
    global device
    try:
        if new_low_vram_value is None:
            raise ValueError("Missing 'low_vram' parameter")

        if params["low_vram"] == new_low_vram_value:
            return Response(
                content=json.dumps(
                    {
                        "status": "success",
                        "message": f"[{params['branding']}Model] LowVRAM is already {'enabled' if new_low_vram_value else 'disabled'}.",
                    }
                )
            )
        params["low_vram"] = new_low_vram_value
        if params["low_vram"]:
            await unload_model(model)
            if torch.cuda.is_available():
                device = "cpu"
                print(
                    f"[{params['branding']}Model] \033[94mChanging model \033[92m(Please wait 15 seconds)\033[0m"
                )
                print(
                    f"[{params['branding']}Model] \033[94mLowVRAM Enabled.\033[0m Model will move between \033[93mVRAM(cuda) <> System RAM(cpu)\033[0m"
                )
                await setup()
            else:
                # Handle the case where CUDA is not available
                print(
                    f"[{params['branding']}Model] \033[91mError:\033[0m Nvidia CUDA is not available on this system. Unable to use LowVRAM mode."
                )
                params["low_vram"] = False
        else:
            await unload_model(model)
            if torch.cuda.is_available():
                device = "cuda"
                print(
                    f"[{params['branding']}Model] \033[94mChanging model \033[92m(Please wait 15 seconds)\033[0m"
                )
                print(
                    f"[{params['branding']}Model] \033[94mLowVRAM Disabled.\033[0m Model will stay in \033[93mVRAM(cuda)\033[0m"
                )
                await setup()
            else:
                # Handle the case where CUDA is not available
                print(
                    f"[{params['branding']}Model] \033[91mError:\033[0m Nvidia CUDA is not available on this system. Unable to use LowVRAM mode."
                )
                params["low_vram"] = False
        return Response(content=json.dumps({"status": "lowvram-success"}))
    except Exception as e:
        return Response(content=json.dumps({"status": "error", "message": str(e)}))


###################
#### DeepSpeed ####
###################
# DEEPSPEED - Reload the model when DeepSpeed checkbox is enabled/disabled
async def handle_deepspeed_change(value):
    global model
    if value:
        # DeepSpeed enabled
        print(f"[{params['branding']}Model] \033[93mDeepSpeed Activating\033[0m")

        print(
            f"[{params['branding']}Model] \033[94mChanging model \033[92m(DeepSpeed can take 30 seconds to activate)\033[0m"
        )
        print(
            f"[{params['branding']}Model] \033[91mInformation\033[0m If you have not set CUDA_HOME path, DeepSpeed may fail to load/activate"
        )
        print(
            f"[{params['branding']}Model] \033[91mInformation\033[0m DeepSpeed needs to find nvcc from the CUDA Toolkit. Please check your CUDA_HOME path is"
        )
        print(
            f"[{params['branding']}Model] \033[91mInformation\033[0m pointing to the correct location and use 'set CUDA_HOME=putyoutpathhere' (Windows) or"
        )
        print(
            f"[{params['branding']}Model] \033[91mInformation\033[0m 'export CUDA_HOME=putyoutpathhere' (Linux) within your Python Environment"
        )
        model = await unload_model(model)
        params["tts_method_api_tts"] = False
        params["tts_method_api_local"] = False
        params["tts_method_xtts_local"] = True
        params["deepspeed_activate"] = True
        await setup()
    else:
        # DeepSpeed disabled
        print(f"[{params['branding']}Model] \033[93mDeepSpeed De-Activating\033[0m")
        print(
            f"[{params['branding']}Model] \033[94mChanging model \033[92m(Please wait 15 seconds)\033[0m"
        )
        params["deepspeed_activate"] = False
        model = await unload_model(model)
        await setup()

    return value  # Return new checkbox value


# DEEPSPEED WEBSERVER- API Enable/Disable DeepSpeed
@app.post("/api/deepspeed")
async def deepspeed(request: Request, new_deepspeed_value: bool):
    try:
        if new_deepspeed_value is None:
            raise ValueError("Missing 'deepspeed' parameter")
        if params["deepspeed_activate"] == new_deepspeed_value:
            return Response(
                content=json.dumps(
                    {
                        "status": "success",
                        "message": f"DeepSpeed is already {'enabled' if new_deepspeed_value else 'disabled'}.",
                    }
                )
            )
        params["deepspeed_activate"] = new_deepspeed_value
        await handle_deepspeed_change(params["deepspeed_activate"])
        return Response(content=json.dumps({"status": "deepspeed-success"}))
    except Exception as e:
        return Response(content=json.dumps({"status": "error", "message": str(e)}))


########################
#### TTS GENERATION ####
########################

# TTS VOICE GENERATION METHODS (called from voice_preview and output_modifer)
async def generate_audio(text, voice, language, temperature, repetition_penalty, output_file, streaming=False, speed=1.0, pitch=0):
    # Get the async generator from the internal function
    response = generate_audio_internal(text, voice, language, temperature, repetition_penalty, output_file, streaming)
    # If streaming, then return the generator as-is, otherwise just exhaust it and return
    if streaming:
        return response
    async for _ in response:
        pass
    
async def generate_audio_local(text, voices, weights, language, temperature, repetition_penalty, output_file, streaming=False, speed=1.0, pitch=0):
    # Get the async generator from the internal function
    response = generate_audio_local_internal(text, voices, weights, language, temperature, repetition_penalty, output_file, streaming, speed, pitch)
    # If streaming, then return the generator as-is, otherwise just exhaust it and return
    if streaming:
        return response
    async for _ in response:
        pass

async def generate_audio_v1(text, voices, weights, language, temperature, repetition_penalty, output_file, streaming=False, speed=1.0, pitch=0):
    # Get the async generator from the internal function
    response = generate_audio_internal_v1(text, voices, weights, language, temperature, repetition_penalty, output_file, streaming, speed, pitch)
    # If streaming, then return the generator as-is, otherwise just exhaust it and return
    if streaming:
        return response
    async for _ in response:
        pass

async def generate_audio_internal(text, voice, language, temperature, repetition_penalty, output_file, streaming, speed=1.0, pitch=0):
    global model
    if params["low_vram"] and device == "cpu":
        await switch_device()
    generate_start_time = time.time()  # Record the start time of generating TTS
    
    # XTTSv2 LOCAL & Xttsv2 FT Method
    if params["tts_method_xtts_local"] or tts_method_xtts_ft:
        print(f"[{params['branding']}TTSGen] {text}")
        gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(
            audio_path=[f"{this_dir}/voices/{voice}"],
            gpt_cond_len=model.config.gpt_cond_len,
            max_ref_length=model.config.max_ref_len,
            sound_norm_refs=model.config.sound_norm_refs,
        )

        # Common arguments for both functions
        common_args = {
            "text": text,
            "language": language,
            "gpt_cond_latent": gpt_cond_latent,
            "speaker_embedding": speaker_embedding,
            "temperature": float(temperature),
            "length_penalty": float(model.config.length_penalty),
            "repetition_penalty": float(repetition_penalty),
            "top_k": int(model.config.top_k),
            "top_p": float(model.config.top_p),
            "enable_text_splitting": True,
            "speed": speed
        }

        # Determine the correct inference function and add streaming specific argument if needed
        inference_func = model.inference_stream if streaming else model.inference
        if streaming:
            common_args["stream_chunk_size"] = 20

        # Call the appropriate function
        output = inference_func(**common_args)

        # Process the output based on streaming or non-streaming
        if streaming:
            # Streaming-specific operations
            file_chunks = []
            wav_buf = io.BytesIO()
            with wave.open(wav_buf, "wb") as vfout:
                vfout.setnchannels(1)
                vfout.setsampwidth(2)
                vfout.setframerate(24000)
                vfout.writeframes(b"")
            wav_buf.seek(0)
            yield wav_buf.read()

            for i, chunk in enumerate(output):
                file_chunks.append(chunk)
                if isinstance(chunk, list):
                    chunk = torch.cat(chunk, dim=0)
                chunk = chunk.clone().detach().cpu().numpy()
                chunk = chunk[None, : int(chunk.shape[0])]
                chunk = np.clip(chunk, -1, 1)
                chunk = (chunk * 32767).astype(np.int16)
                yield chunk.tobytes()
        else:
            # Non-streaming-specific operation
            # torchaudio.save(output_file, torch.tensor(output["wav"]).unsqueeze(0), 24000)

            wav_output_file = output_file.replace('mp3', 'wav')
            torchaudio.save(wav_output_file, torch.tensor(output["wav"]).unsqueeze(0), 24000)
            if pitch != 0:
                
                audio = AudioSegment.from_wav(wav_output_file)
                octaves = pitch / 12
                new_sample_rate = int(audio.frame_rate * (2 ** octaves))
                hipitched_sound = audio._spawn(audio.raw_data, overrides={'frame_rate': new_sample_rate})
                hipitched_sound = hipitched_sound.set_frame_rate(24000)  # 假设您想要的最终采样率是24000Hz

                # 直接导出为MP3，不需要转回WAV然后再到MP3
                hipitched_sound.export(output_file, format="mp3")
                

            else:
                AudioSegment.from_wav(wav_output_file).export(output_file, format="mp3")





    
    # API LOCAL Methods
    elif params["tts_method_api_local"]:
        # Streaming only allowed for XTTSv2 local
        if streaming:
            raise ValueError("Streaming is only supported in XTTSv2 local")

        # Set the correct output path (different from the if statement)
        print(f"[{params['branding']}TTSGen] Using API Local")
        model.tts_to_file(
            text=text,
            file_path=output_file,
            speaker_wav=[f"{this_dir}/voices/{voice}"],
            language=language,
            temperature=temperature,
            length_penalty=model.config.length_penalty,
            repetition_penalty=repetition_penalty,
            top_k=model.config.top_k,
            top_p=model.config.top_p,
        )

    # API TTS
    elif params["tts_method_api_tts"]:
        # Streaming only allowed for XTTSv2 local
        if streaming:
            raise ValueError("Streaming is only supported in XTTSv2 local")

        print(f"[{params['branding']}TTSGen] Using API TTS")
        model.tts_to_file(
            text=text,
            file_path=output_file,
            speaker_wav=[f"{this_dir}/voices/{voice}"],
            language=language,
        )

    # Print Generation time and settings
    generate_end_time = time.time()  # Record the end time to generate TTS
    generate_elapsed_time = generate_end_time - generate_start_time
    print(
        f"[{params['branding']}TTSGen] \033[93m{generate_elapsed_time:.2f} seconds. \033[94mLowVRAM: \033[33m{params['low_vram']} \033[94mDeepSpeed: \033[33m{params['deepspeed_activate']}\033[0m"
    )
    # Move model back to cpu system ram if needed.
    if params["low_vram"] and device == "cuda":
        await switch_device()
    return



async def generate_audio_local_internal(text, voices, weights, language, temperature, repetition_penalty, output_file, streaming, speed, pitch):
    global model
    if params["low_vram"] and device == "cpu":
        await switch_device()
    generate_start_time = time.time()  # Record the start time of generating TTS
    
    # XTTSv2 LOCAL & Xttsv2 FT Method
    if params["tts_method_xtts_local"] or tts_method_xtts_ft:
        print(f"[{params['branding']}TTSGen] {text}")

        #读取角色信息，加权求和再平均得到目标音色变量（weighted_gpt_cond_latent, weighted_speaker_embedding）
        with open(this_dir / "data.json", 'r') as file:
            information = json.load(file)
            gpt_cond_latents = []
            speaker_embeddings = []
            for i in range(len(voices)):
                gpt_cond_latent, speaker_embedding = information[voices[i]]["gpt_cond_latent"], information[voices[i]]["speaker_embedding"]
                gpt_cond_latent, speaker_embedding = torch.tensor(gpt_cond_latent).to(device), torch.tensor(speaker_embedding).to(device)
                gpt_cond_latents.append(gpt_cond_latent)
                speaker_embeddings.append(speaker_embedding)
            weighted_speaker_embeddings = [speaker_embedding * weight for speaker_embedding, weight in zip(speaker_embeddings, weights)]
            weighted_gpt_cond_latents = [gpt_cond_latent * weight for gpt_cond_latent, weight in zip(gpt_cond_latents, weights)]
            weighted_speaker_embedding = torch.mean(torch.stack(weighted_speaker_embeddings), dim=0)
            weighted_gpt_cond_latent = torch.mean(torch.stack(weighted_gpt_cond_latents), dim=0)


        # Common arguments for both functions
        common_args = {
            "text": text,
            "language": language,
            "gpt_cond_latent": weighted_gpt_cond_latent,
            "speaker_embedding": weighted_speaker_embedding,
            "temperature": float(temperature),
            "length_penalty": float(model.config.length_penalty),
            "repetition_penalty": float(repetition_penalty),
            "top_k": int(model.config.top_k),
            "top_p": float(model.config.top_p),
            "enable_text_splitting": True,
            "speed": speed
        }

        # Determine the correct inference function and add streaming specific argument if needed
        inference_func = model.inference_stream if streaming else model.inference
        if streaming:
            common_args["stream_chunk_size"] = 20

        # Call the appropriate function
        output = inference_func(**common_args)

        # Process the output based on streaming or non-streaming
        if streaming:
            # Streaming-specific operations
            file_chunks = []
            wav_buf = io.BytesIO()
            with wave.open(wav_buf, "wb") as vfout:
                vfout.setnchannels(1)
                vfout.setsampwidth(2)
                vfout.setframerate(24000)
                vfout.writeframes(b"")
            wav_buf.seek(0)
            yield wav_buf.read()

            for i, chunk in enumerate(output):
                file_chunks.append(chunk)
                if isinstance(chunk, list):
                    chunk = torch.cat(chunk, dim=0)
                chunk = chunk.clone().detach().cpu().numpy()
                chunk = chunk[None, : int(chunk.shape[0])]
                chunk = np.clip(chunk, -1, 1)
                chunk = (chunk * 32767).astype(np.int16)
                yield chunk.tobytes()
        else:
            # Non-streaming-specific operation
            wav_output_file = output_file.replace('mp3', 'wav')
            torchaudio.save(wav_output_file, torch.tensor(output["wav"]).unsqueeze(0), 24000)
            if pitch != 0:
                
                """
                #读取音频文件，调用pydub库
                #根据音调变化改变采样率
                audio = AudioSegment.from_wav(wav_output_file)
                octaves = pitch / 12
                new_sample_rate = int(audio.frame_rate * (2 ** octaves))
                hipitched_sound = audio._spawn(audio.raw_data, overrides={'frame_rate': new_sample_rate})
                hipitched_sound = hipitched_sound.set_frame_rate(audio.frame_rate)
                speed = hipitched_sound.duration_seconds/ audio.duration_seconds
                hipitched_sound.export(wav_output_file+"1", format="wav")

                #调用pyrubberband库
                #将音调改变的文件改回原来的时长
                y, sr = librosa.load(wav_output_file+"1", sr=None)
                y_stretched = pyrubberband.time_stretch(y, sr, speed)
                sf.write(output_file, y_stretched, sr, format='mp3')
                
                #######################
                """
                audio = AudioSegment.from_wav(wav_output_file)
                octaves = pitch / 12
                new_sample_rate = int(audio.frame_rate * (2 ** octaves))
                hipitched_sound = audio._spawn(audio.raw_data, overrides={'frame_rate': new_sample_rate})
                hipitched_sound = hipitched_sound.set_frame_rate(24000)  # 假设您想要的最终采样率是24000Hz

                # 直接导出为MP3，不需要转回WAV然后再到MP3
                hipitched_sound.export(output_file, format="mp3")
            
            else:
                AudioSegment.from_wav(wav_output_file).export(output_file, format="mp3")



    
    # API LOCAL Methods
    elif params["tts_method_api_local"]:
        # Streaming only allowed for XTTSv2 local
        if streaming:
            raise ValueError("Streaming is only supported in XTTSv2 local")

        # Set the correct output path (different from the if statement)
        print(f"[{params['branding']}TTSGen] Using API Local")
        model.tts_to_file(
            text=text,
            file_path=output_file,
            speaker_wav=[f"{this_dir}/voices/{voice}"],
            language=language,
            temperature=temperature,
            length_penalty=model.config.length_penalty,
            repetition_penalty=repetition_penalty,
            top_k=model.config.top_k,
            top_p=model.config.top_p,
        )

    # API TTS
    elif params["tts_method_api_tts"]:
        # Streaming only allowed for XTTSv2 local
        if streaming:
            raise ValueError("Streaming is only supported in XTTSv2 local")

        print(f"[{params['branding']}TTSGen] Using API TTS")
        model.tts_to_file(
            text=text,
            file_path=output_file,
            speaker_wav=[f"{this_dir}/voices/{voice}"],
            language=language,
        )

    # Print Generation time and settings
    generate_end_time = time.time()  # Record the end time to generate TTS
    generate_elapsed_time = generate_end_time - generate_start_time
    print(
        f"[{params['branding']}TTSGen] \033[93m{generate_elapsed_time:.2f} seconds. \033[94mLowVRAM: \033[33m{params['low_vram']} \033[94mDeepSpeed: \033[33m{params['deepspeed_activate']}\033[0m"
    )
    # Move model back to cpu system ram if needed.
    if params["low_vram"] and device == "cuda":
        await switch_device()
    return



async def generate_audio_internal_v1(text, voices, weights, language, temperature, repetition_penalty, output_file, streaming, speed, pitch):
    global model
    if params["low_vram"] and device == "cpu":
        await switch_device()
    generate_start_time = time.time()  # Record the start time of generating TTS
    
    # XTTSv2 LOCAL & Xttsv2 FT Method
    if params["tts_method_xtts_local"] or tts_method_xtts_ft:
        print(f"[{params['branding']}TTSGen] {text}")

        #加权求和再平均得到目标音色变量（weighted_gpt_cond_latent, weighted_speaker_embedding）
        gpt_cond_latents = []
        speaker_embeddings = []
        for voice_url in voices:
            voice = check_or_download_voice(voice_url)
            gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(
                audio_path=[voice],
                gpt_cond_len=model.config.gpt_cond_len,
                max_ref_length=model.config.max_ref_len,
                sound_norm_refs=model.config.sound_norm_refs,
            )

            gpt_cond_latent, speaker_embedding = torch.tensor(gpt_cond_latent).to(device), torch.tensor(speaker_embedding).to(device)
            gpt_cond_latents.append(gpt_cond_latent)
            speaker_embeddings.append(speaker_embedding)

        weighted_speaker_embeddings = [speaker_embedding * weight for speaker_embedding, weight in zip(speaker_embeddings, weights)]
        weighted_gpt_cond_latents = [gpt_cond_latent * weight for gpt_cond_latent, weight in zip(gpt_cond_latents, weights)]
        weighted_speaker_embedding = torch.mean(torch.stack(weighted_speaker_embeddings), dim=0)
        weighted_gpt_cond_latent = torch.mean(torch.stack(weighted_gpt_cond_latents), dim=0)    

        # Common arguments for both functions
        common_args = {
            "text": text,
            "language": language,
            "gpt_cond_latent": weighted_gpt_cond_latent,
            "speaker_embedding": weighted_speaker_embedding,
            "temperature": float(temperature),
            "length_penalty": float(model.config.length_penalty),
            "repetition_penalty": float(repetition_penalty),
            "top_k": int(model.config.top_k),
            "top_p": float(model.config.top_p),
            "enable_text_splitting": True,
            "speed": speed
        }


        # print(f"[{params['branding']}TTSGen] {text}")
        # gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(
        #     audio_path=[f"{this_dir}/voices/{voices[0]}"],
        #     gpt_cond_len=model.config.gpt_cond_len,
        #     max_ref_length=model.config.max_ref_len,
        #     sound_norm_refs=model.config.sound_norm_refs,
        # )

        # # Common arguments for both functions
        # common_args = {
        #     "text": text,
        #     "language": language,
        #     "gpt_cond_latent": gpt_cond_latent,
        #     "speaker_embedding": speaker_embedding,
        #     "temperature": float(temperature),
        #     "length_penalty": float(model.config.length_penalty),
        #     "repetition_penalty": float(repetition_penalty),
        #     "top_k": int(model.config.top_k),
        #     "top_p": float(model.config.top_p),
        #     "enable_text_splitting": True,
        #     "speed": speed
        # }

        # Determine the correct inference function and add streaming specific argument if needed
        inference_func = model.inference_stream if streaming else model.inference
        if streaming:
            common_args["stream_chunk_size"] = 20

        # Call the appropriate function
        output = inference_func(**common_args)

        # Process the output based on streaming or non-streaming
        if streaming:
            # Streaming-specific operations
            file_chunks = []
            wav_buf = io.BytesIO()
            with wave.open(wav_buf, "wb") as vfout:
                vfout.setnchannels(1)
                vfout.setsampwidth(2)
                vfout.setframerate(24000)
                vfout.writeframes(b"")
            wav_buf.seek(0)
            yield wav_buf.read()

            for i, chunk in enumerate(output):
                file_chunks.append(chunk)
                if isinstance(chunk, list):
                    chunk = torch.cat(chunk, dim=0)
                chunk = chunk.clone().detach().cpu().numpy()
                chunk = chunk[None, : int(chunk.shape[0])]
                chunk = np.clip(chunk, -1, 1)
                chunk = (chunk * 32767).astype(np.int16)
                yield chunk.tobytes()
        else:
            # Non-streaming-specific operation
            wav_output_file = output_file.replace('mp3', 'wav')
            torchaudio.save(wav_output_file, torch.tensor(output["wav"]).unsqueeze(0), 24000)
            if pitch != 0:
                #最初版本，没有升降速
                                
                # audio = AudioSegment.from_wav(wav_output_file)
                # octaves = pitch / 12
                # new_sample_rate = int(audio.frame_rate * (2 ** octaves))
                # hipitched_sound = audio._spawn(audio.raw_data, overrides={'frame_rate': new_sample_rate})
                # hipitched_sound = hipitched_sound.set_frame_rate(24000)  # 假设您想要的最终采样率是24000Hz

                # # 直接导出为MP3，不需要转回WAV然后再到MP3
                # hipitched_sound.export(output_file, format="mp3")
                

                #lhr版本，根据pitch自适应升降速，保持语音时长不变
                
                speed = 1 / (2 ** (pitch / 12))
                # 读取MP3文件并转换为WAV格式
                audio = AudioSegment.from_wav(wav_output_file)
                octaves = pitch / 12
                new_sample_rate = int(audio.frame_rate * (2 ** octaves))
                hipitched_sound = audio._spawn(audio.raw_data, overrides={'frame_rate': new_sample_rate})
                hipitched_sound = hipitched_sound.set_frame_rate(24000)  # 设置最终采样率为24000Hz

                # 使用pyrubberband处理时间拉伸
                y, sr = librosa.load(hipitched_sound.export(format="wav"), sr=None)
                y_stretched = pyrubberband.time_stretch(y, sr, speed)
                # 直接导出为MP3，不需要转回WAV然后再到MP3
                # hipitched_sound.export(output_file, format="mp3")
                # y_stretched.export(output_file, format="mp3")
                # 保存处理后的音频为MP3
                sf.write(output_file, y_stretched, sr, format='wav')
                final_audio = AudioSegment.from_wav(output_file)
                final_audio.export(output_file, format="mp3")


                
            else:
                AudioSegment.from_wav(wav_output_file).export(output_file, format="mp3")



    
    # API LOCAL Methods
    elif params["tts_method_api_local"]:
        # Streaming only allowed for XTTSv2 local
        if streaming:
            raise ValueError("Streaming is only supported in XTTSv2 local")

    

        # Set the correct output path (different from the if statement)
        print(f"[{params['branding']}TTSGen] Using API Local")
        model.tts_to_file(
            text=text,
            file_path=output_file,
            speaker_wav=[f"{this_dir}/voices/{voice}"],
            language=language,
            temperature=temperature,
            length_penalty=model.config.length_penalty,
            repetition_penalty=repetition_penalty,
            top_k=model.config.top_k,
            top_p=model.config.top_p,
        )

    # API TTS
    elif params["tts_method_api_tts"]:
        # Streaming only allowed for XTTSv2 local
        if streaming:
            raise ValueError("Streaming is only supported in XTTSv2 local")

        print(f"[{params['branding']}TTSGen] Using API TTS")
        model.tts_to_file(
            text=text,
            file_path=output_file,
            speaker_wav=[f"{this_dir}/voices/{voice}"],
            language=language,
        )

    # Print Generation time and settings
    generate_end_time = time.time()  # Record the end time to generate TTS
    generate_elapsed_time = generate_end_time - generate_start_time
    print(
        f"[{params['branding']}TTSGen] \033[93m{generate_elapsed_time:.2f} seconds. \033[94mLowVRAM: \033[33m{params['low_vram']} \033[94mDeepSpeed: \033[33m{params['deepspeed_activate']}\033[0m"
    )
    # Move model back to cpu system ram if needed.
    if params["low_vram"] and device == "cuda":
        await switch_device()
    return



# TTS VOICE GENERATION METHODS - generate TTS API
@app.route("/api/generate", methods=["POST"])
async def generate(request: Request):
    try:
        # Get parameters from JSON body
        data = await request.json()
        text = data["text"]
        voice = data["voice"]
        language = data["language"]
        temperature = data["temperature"]
        repetition_penalty = data["repetition_penalty"]
        output_file = data["output_file"]
        streaming = False
        # Generation logic
        response = await generate_audio(text, voice, language, temperature, repetition_penalty, output_file, streaming)
        if streaming:
            return StreamingResponse(response, media_type="audio/wav")
        return JSONResponse(
            content={"status": "generate-success", "data": {"audio_path": output_file}}
        )
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": str(e)})

def extract_and_concatenate_quoted_text(text):
    """
    提取字符串中所有双引号内的文本并拼接，如果没有双引号内的文本，则返回原文。
    参数:
    text (str): 需要处理的文本。
    返回:
    str: 拼接后的双引号内文本或原文。
    """
    matches = re.findall(r'"(.*?)"', text)
    if matches:
        # 如果找到双引号内的文本，则将它们拼接起来
        return ' '.join(matches)
    else:
        # 如果没有找到双引号内的文本，返回原文
        return text

def clean_old_files(folder_path: Path, keep_latest: int = 10):
    """
    清理指定目录下的旧文件，只保留最新的N个文件。

    参数:
    - folder_path: 要清理的目录的路径，应为Path对象。
    - keep_latest: 需要保留的最新文件数量，默认为10。
    """
    # 确保folder_path是Path对象
    if not isinstance(folder_path, Path):
        folder_path = Path(folder_path)
    
    # 获取目录中的所有文件
    files = list(folder_path.iterdir())

    # 获取文件及其修改时间
    files_with_time = [(file, file.stat().st_mtime) for file in files if file.is_file()]

    # 按修改时间对文件进行排序
    files_with_time.sort(key=lambda x: x[1], reverse=True)

    # 保留最新的N个文件，删除其他文件
    for file, _ in files_with_time[keep_latest:]:
        try:
            file.unlink()
        except Exception as e:
            print(f"Error deleting file {file}: {e}")


@app.route("/api/generate_local", methods=["POST"])
async def generate_local(request: Request):
    try:
        # Get parameters from JSON body
        data = await request.json()
        #text = extract_and_concatenate_quoted_text(data["text"])
        text = data["text"]
        voices = data["voices"]
        weights = data["weights"]
        if len(voices) != len(weights):
            raise ValueError("Weights doesn't match the number of voices for weighted average.")
        language = "en"     #data["language"]
        temperature = 0.75  #data["temperature"]
        repetition_penalty = 10 #data["repetition_penalty"]

        now = datetime.now()
        date_string = now.strftime("%Y-%m-%d")
        characters = string.ascii_letters + string.digits
        folder_path = this_dir / "outputs" # + date_string
        print(folder_path)
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)

        random_string = ''.join(random.choice(characters) for i in range(32))
        filename = "{}.mp3".format(random_string)
        output_file = "{}/{}".format(folder_path, filename)

        clean_old_files(folder_path, 10)

        # output_file = data["output_file"]
        streaming = False
        pitch = data["pitch"]
        speed = data["speed"]
        if speed == 0:
            speed = 1
        # Generation logic
        print("voices:{}, weights:{}, language:{}, speed:{}, pitch:{}".format(voices, weights, language, speed, pitch))
        response = await generate_audio_local(text, voices, weights, language, temperature, repetition_penalty, output_file, streaming, speed, pitch)
        if streaming:
            return StreamingResponse(response, media_type="audio/wav")
        return JSONResponse(
            content={"status": "generate-success", "data": {"audio_path": filename}}
        )
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": str(e)})


# TTS VOICE GENERATION METHODS - generate TTS API
    
@app.route("/api/v1/tts", methods=["POST"])
async def generate_v1(request: Request):
    try:
        # Get parameters from JSON body
        data = await request.json()
        text = data["text"]
        voices = data["voices"]
        weights = data["weights"]
        if len(voices) != len(weights):
            raise ValueError("Weights doesn't match the number of voices for weighted average.")
        language = "en"     #data["language"]
        temperature = 0.75  #data["temperature"]
        repetition_penalty = 10 #data["repetition_penalty"]

        now = datetime.now()
        date_string = now.strftime("%Y-%m-%d")
        characters = string.ascii_letters + string.digits
        folder_path = this_dir / "outputs" # + date_string
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)

        random_string = ''.join(random.choice(characters) for i in range(32))
        filename = "{}.mp3".format(random_string)
        output_file = "{}/{}".format(folder_path, filename)

        clean_old_files(folder_path, 10)

        # output_file = data["output_file"]
        streaming = False
        pitch = data["pitch"]
        speed = data["speed"]
        if speed == 0:
            speed = 1
        # Generation logic
        print("voices:{}, weights:{}, language:{}, speed:{}, pitch:{}".format(voices, weights, language, speed, pitch))
        response = await generate_audio_v1(text, voices, weights, language, temperature, repetition_penalty, output_file, streaming, speed, pitch)
        if streaming:
            return StreamingResponse(response, media_type="audio/wav")
        return JSONResponse(
            content={"status": "generate-success", "data": {"audio_path": filename}}
        )
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": str(e)})


@app.route("/api/v2/tts", methods=["POST"])
async def generate_v2(request: Request):
    data = await request.json()

    # Extract fields from the data
    voice = data.get("voice")
    prompt_text = data.get("prompt_text")
    text = data.get("text")
    gpt_model = data.get("gpt_model")
    sovits_model = data.get("sovits_model")
    
    # Prepare the payload for the forwarding request
    payload = {
        "voice": voice,
        "prompt_text": prompt_text,
        "text": text,
        "gpt_model": gpt_model,
        "sovits_model": sovits_model,
        "output_directory": f"{this_dir}/outputs/"
    }
    
    try:
        # Forward the request to the other endpoint with increased timeout
        async with httpx.AsyncClient(timeout=60.0) as client:  # Set timeout to 30 seconds
            response = await client.post("http://127.0.0.1:9880", json=payload)
            response.raise_for_status()  # Raise an exception for HTTP errors

        # Return the response from the forwarded request as JSON
        return JSONResponse(status_code=response.status_code, content=response.json())

    except httpx.RequestError as exc:
        # Handle request errors, including timeout
        raise HTTPException(status_code=500, detail=str(exc))
    except httpx.HTTPStatusError as exc:
        # Handle HTTP errors
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)


@app.route("/api/v2/training", methods=["POST"])
async def training_v2(request: Request):
    data = await request.json()
    
    # Prepare the payload for the forwarding request
    payload = {
        "voice": data.get("voice"),
        "name": data.get("name")
    }

    try:
        # Forward the request to the other endpoint with increased timeout
        async with httpx.AsyncClient(timeout=300.0) as client:  # Set timeout to 30 seconds
            response = await client.post("http://127.0.0.1:9880/training", json=payload)
            response.raise_for_status()  # Raise an exception for HTTP errors
            response_data = response.json()

            folder_path = this_dir / "outputs" # + date_string
            if not os.path.exists(folder_path):
                os.makedirs(folder_path)
                
            filename = f"{int(time.time())}_{uuid.uuid4().hex}.mp3"
            output_file = "{}/{}".format(folder_path, filename)

            voice_ref = response_data["data"]["voice_ref"]
            shutil.copy(voice_ref, output_file)

            response_data["data"]["voice_ref"] = filename

        # Return the response from the forwarded request as JSON
        return JSONResponse(status_code=response.status_code, content=response_data)

    except httpx.RequestError as exc:
        # Handle request errors, including timeout
        raise HTTPException(status_code=500, detail=str(exc))
    except httpx.HTTPStatusError as exc:
        # Handle HTTP errors
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)


###################################################
#### POPULATE FILES LIST FROM VOICES DIRECTORY ####
###################################################
# List files in the "voices" directory
def list_files(directory):
    files = [
        f
        for f in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, f)) and f.endswith(".wav")
    ]
    return files

#############################
#### JSON CONFIG UPDATER ####
#############################

# Create an instance of Jinja2Templates for rendering HTML templates
templates = Jinja2Templates(directory=this_dir / "templates")

# Create a dependency to get the current JSON data
def get_json_data():
    with open(this_dir / "confignew.json", "r") as json_file:
        data = json.load(json_file)
    return data


# Define an endpoint function
@app.get("/settings")
async def get_settings(request: Request):
    wav_files = list_files(this_dir / "voices")
    # Render the template with the current JSON data and list of WAV files
    return templates.TemplateResponse(
        "generate_form.html",
        {
            "request": request,
            "data": get_json_data(),
            "modeldownload_model_path": modeldownload_model_path,
            "wav_files": wav_files,
        },
    )

# Define an endpoint to serve static files
app.mount("/static", StaticFiles(directory=str(this_dir / "templates")), name="static")

@app.post("/update-settings")
async def update_settings(
    request: Request,
    activate: bool = Form(...),
    autoplay: bool = Form(...),
    deepspeed_activate: bool = Form(...),
    delete_output_wavs: str = Form(...),
    ip_address: str = Form(...),
    language: str = Form(...),
    local_temperature: str = Form(...),
    local_repetition_penalty: str = Form(...),
    low_vram: bool = Form(...),
    tts_model_loaded: bool = Form(...),
    tts_model_name: str = Form(...),
    narrator_enabled: bool = Form(...),
    narrator_voice: str = Form(...),
    output_folder_wav: str = Form(...),
    port_number: str = Form(...),
    remove_trailing_dots: bool = Form(...),
    show_text: bool = Form(...),
    tts_method: str = Form(...),
    voice: str = Form(...),
    data: dict = Depends(get_json_data),
):
    # Update the settings based on the form values
    data["activate"] = activate
    data["autoplay"] = autoplay
    data["deepspeed_activate"] = deepspeed_activate
    data["delete_output_wavs"] = delete_output_wavs
    data["ip_address"] = ip_address
    data["language"] = language
    data["local_temperature"] = local_temperature
    data["local_repetition_penalty"] = local_repetition_penalty
    data["low_vram"] = low_vram
    data["tts_model_loaded"] = tts_model_loaded
    data["tts_model_name"] = tts_model_name
    data["narrator_enabled"] = narrator_enabled
    data["narrator_voice"] = narrator_voice
    data["output_folder_wav"] = output_folder_wav
    data["port_number"] = port_number
    data["remove_trailing_dots"] = remove_trailing_dots
    data["show_text"] = show_text
    data["tts_method_api_local"] = tts_method == "api_local"
    data["tts_method_api_tts"] = tts_method == "api_tts"
    data["tts_method_xtts_local"] = tts_method == "xtts_local"
    data["voice"] = voice

    # Save the updated settings back to the JSON file
    with open(this_dir / "confignew.json", "w") as json_file:
        json.dump(data, json_file)

    # Redirect to the settings page to display the updated settings
    return RedirectResponse(url="/settings", status_code=303)


##################################
#### SETTINGS PAGE DEMO VOICE ####
##################################

@app.get("/tts-demo-request", response_class=StreamingResponse)
async def tts_demo_request_streaming(text: str, voice: str, language: str, output_file: str):
    try:
        output_file_path = this_dir / "outputs" / output_file
        stream = await generate_audio(text, voice, language, temperature, repetition_penalty, output_file_path, streaming=True)
        return StreamingResponse(stream, media_type="audio/wav")
    except Exception as e:
        print(f"An error occurred: {e}")
        return JSONResponse(content={"error": "An error occurred"}, status_code=500)

@app.post("/tts-demo-request", response_class=JSONResponse)
async def tts_demo_request(request: Request, text: str = Form(...), voice: str = Form(...), language: str = Form(...), output_file: str = Form(...)):
    try:
        output_file_path = this_dir / "outputs" / output_file
        await generate_audio(text, voice, language, temperature, repetition_penalty, output_file_path, streaming=False)
        return JSONResponse(content={"output_file_path": str(output_file)}, status_code=200)
    except Exception as e:
        print(f"An error occurred: {e}")
        return JSONResponse(content={"error": "An error occurred"}, status_code=500)


#####################
#### Audio feeds ####
#####################

# Gives web access to the output files
@app.get("/audio/{filename}")
async def get_audio(filename: str):
    audio_path = this_dir / "outputs" / filename
    return FileResponse(audio_path)

@app.get("/audiocache/{filename}")
async def get_audio(filename: str):
    audio_path = Path("outputs") / filename
    if not audio_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    
    response = FileResponse(
        path=audio_path,
        media_type='audio/wav',
        filename=filename
    )
    # Set caching headers
    response.headers["Cache-Control"] = "public, max-age=604800"  # Cache for one week
    response.headers["ETag"] = str(audio_path.stat().st_mtime)  # Use the file's last modified time as a simple ETag

    return response

#########################
#### VOICES LIST API ####
#########################
# Define the new endpoint
@app.get("/api/voices")
async def get_voices():
    wav_files = list_files(this_dir / "voices")
    return {"voices": wav_files}

###########################
#### PREVIEW VOICE API ####
###########################
@app.post("/api/previewvoice/", response_class=JSONResponse)
async def preview_voice(request: Request, voice: str = Form(...)):
    try:
        # Hardcoded settings
        language = "en"
        output_file_name = "api_preview_voice"

        # Clean the voice filename for inclusion in the text
        clean_voice_filename = re.sub(r'\.wav$', '', voice.replace(' ', '_'))
        clean_voice_filename = re.sub(r'[^a-zA-Z0-9]', ' ', clean_voice_filename)
        
        # Generate the audio
        text = f"Hello, this is a preview of voice {clean_voice_filename}."

        # Generate the audio
        output_file_path = this_dir / "outputs" / f"{output_file_name}.wav"
        await generate_audio(text, voice, language, temperature, repetition_penalty, output_file_path, streaming=False)

        # Generate the URL
        output_file_url = f'http://{params["ip_address"]}:{params["port_number"]}/audio/{output_file_name}.wav'

        # Return the response with both local file path and URL
        return JSONResponse(
            content={
                "status": "generate-success",
                "output_file_path": str(output_file_path),
                "output_file_url": str(output_file_url),
            },
            status_code=200,
        )
    except Exception as e:
        print(f"An error occurred: {e}")
        return JSONResponse(content={"error": "An error occurred"}, status_code=500)

########################
#### GENERATION API ####
########################
import html
import re
import uuid
import numpy as np
import soundfile as sf
import sys
import hashlib

##############################
#### Streaming Generation ####
##############################

@app.get("/api/tts-generate-streaming", response_class=StreamingResponse)
async def tts_generate_streaming(text: str, voice: str, language: str, output_file: str):
    try:
        output_file_path = this_dir / "outputs" / output_file
        stream = await generate_audio(text, voice, language, temperature, repetition_penalty, output_file_path, streaming=True)
        return StreamingResponse(stream, media_type="audio/wav")
    except Exception as e:
        print(f"An error occurred: {e}")
        return JSONResponse(content={"error": "An error occurred"}, status_code=500)

@app.post("/api/tts-generate-streaming", response_class=JSONResponse)
async def tts_generate_streaming(request: Request, text: str = Form(...), voice: str = Form(...), language: str = Form(...), output_file: str = Form(...)):
    try:
        output_file_path = this_dir / "outputs" / output_file
        await generate_audio(text, voice, language, temperature, repetition_penalty, output_file_path, streaming=False)
        return JSONResponse(content={"output_file_path": str(output_file)}, status_code=200)
    except Exception as e:
        print(f"An error occurred: {e}")
        return JSONResponse(content={"error": "An error occurred"}, status_code=500)

##############################
#### Standard Generation ####
##############################

# Check for PortAudio library on Linux
try:
    import sounddevice as sd
    sounddevice_installed=True
except OSError:
    print(f"[{params['branding']}Startup] \033[91mInfo\033[0m PortAudio library not found. If you wish to play TTS in standalone mode through the API suite")
    print(f"[{params['branding']}Startup] \033[91mInfo\033[0m please install PortAudio. This will not affect any other features or use of Alltalk.")
    print(f"[{params['branding']}Startup] \033[91mInfo\033[0m If you don't know what the API suite is, then this message is nothing to worry about.")
    sounddevice_installed=False
    if sys.platform.startswith('linux'):
        print(f"[{params['branding']}Startup] \033[91mInfo\033[0m On Linux, you can use the following command to install PortAudio:")
        print(f"[{params['branding']}Startup] \033[91mInfo\033[0m sudo apt-get install portaudio19-dev")

from typing import Union, Dict
from pydantic import BaseModel, ValidationError, Field

def play_audio(file_path, volume):
    data, fs = sf.read(file_path)
    sd.play(volume * data, fs)
    sd.wait()

class Request(BaseModel):
    # Define the structure of the 'Request' class if needed
    pass

class JSONInput(BaseModel):
    text_input: str = Field(..., max_length=2000, description="text_input needs to be 2000 characters or less.")
    text_filtering: str = Field(..., pattern="^(none|standard|html)$", description="text_filtering needs to be 'none', 'standard' or 'html'.")
    character_voice_gen: str = Field(..., pattern="^.*\.wav$", description="character_voice_gen needs to be the name of a wav file e.g. mysample.wav.")
    narrator_enabled: bool = Field(..., description="narrator_enabled needs to be true or false.")
    narrator_voice_gen: str = Field(..., pattern="^.*\.wav$", description="narrator_voice_gen needs to be the name of a wav file e.g. mysample.wav.")
    text_not_inside: str = Field(..., pattern="^(character|narrator)$", description="text_not_inside needs to be 'character' or 'narrator'.")
    language: str = Field(..., pattern="^(ar|zh-cn|cs|nl|en|fr|de|hu|it|ja|ko|pl|pt|ru|es|tr)$", description="language needs to be one of the following ar|zh-cn|cs|nl|en|fr|de|hu|it|ja|ko|pl|pt|ru|es|tr.")
    output_file_name: str = Field(..., pattern="^[a-zA-Z0-9_]+$", description="output_file_name needs to be the name without any special characters or file extension e.g. 'filename'")
    output_file_timestamp: bool = Field(..., description="output_file_timestamp needs to be true or false.")
    autoplay: bool = Field(..., description="autoplay needs to be a true or false value.")
    autoplay_volume: float = Field(..., ge=0.1, le=1.0, description="autoplay_volume needs to be from 0.1 to 1.0")

    @classmethod
    def validate_autoplay_volume(cls, value):
        if not (0.1 <= value <= 1.0):
            raise ValueError("Autoplay volume must be between 0.1 and 1.0")
        return value


class TTSGenerator:
    @staticmethod
    def validate_json_input(json_data: Union[Dict, str]) -> Union[None, str]:
        try:
            if isinstance(json_data, str):
                json_data = json.loads(json_data)
            JSONInput(**json_data)
            return None  # JSON is valid
        except ValidationError as e:
            return str(e)

def process_text(text):
    # Normalize HTML encoded quotes
    text = html.unescape(text)
    # Replace ellipsis with a single dot
    text = re.sub(r'\.{3,}', '.', text)
    # Pattern to identify combined narrator and character speech
    combined_pattern = r'(\*[^*"]+\*|"[^"*]+")'
    # List to hold parts of speech along with their type
    ordered_parts = []
    # Track the start of the next segment
    start = 0
    # Find all matches
    for match in re.finditer(combined_pattern, text):
        # Add the text before the match, if any, as ambiguous
        if start < match.start():
            ambiguous_text = text[start:match.start()].strip()
            if ambiguous_text:
                ordered_parts.append(('ambiguous', ambiguous_text))
        # Add the matched part as either narrator or character
        matched_text = match.group(0)
        if matched_text.startswith('*') and matched_text.endswith('*'):
            ordered_parts.append(('narrator', matched_text.strip('*').strip()))
        elif matched_text.startswith('"') and matched_text.endswith('"'):
            ordered_parts.append(('character', matched_text.strip('"').strip()))
        else:
            # In case of mixed or improperly formatted parts
            if '*' in matched_text:
                ordered_parts.append(('narrator', matched_text.strip('*').strip('"')))
            else:
                ordered_parts.append(('character', matched_text.strip('"').strip('*')))
        # Update the start of the next segment
        start = match.end()
    # Add any remaining text after the last match as ambiguous
    if start < len(text):
        ambiguous_text = text[start:].strip()
        if ambiguous_text:
            ordered_parts.append(('ambiguous', ambiguous_text))
    return ordered_parts

def standard_filtering(text_input):
    text_output = (text_input
                        .replace("***", "")
                        .replace("**", "")
                        .replace("*", "")
                        .replace("\n\n", "\n")
                        .replace("&#x27;", "'")
                        )
    return text_output

def combine(output_file_timestamp, output_file_name, audio_files):
    audio = np.array([])
    sample_rate = None
    try:
        for audio_file in audio_files:
            audio_data, current_sample_rate = sf.read(audio_file)
            if audio.size == 0:
                audio = audio_data
                sample_rate = current_sample_rate
            elif sample_rate == current_sample_rate:
                audio = np.concatenate((audio, audio_data))
            else:
                raise ValueError("Sample rates of input files are not consistent.")
    except Exception as e:
        # Handle exceptions (e.g., file not found, invalid audio format)
        return None, None
    if output_file_timestamp:
        timestamp = int(time.time())
        output_file_path = os.path.join(this_dir / "outputs" / f'{output_file_name}_{timestamp}_combined.wav')
        output_file_url = f'http://{params["ip_address"]}:{params["port_number"]}/audio/{output_file_name}_{timestamp}_combined.wav'
        output_cache_url = f'http://{params["ip_address"]}:{params["port_number"]}/audiocache/{output_file_name}_{timestamp}_combined.wav'
    else:
        output_file_path = os.path.join(this_dir / "outputs" / f'{output_file_name}_combined.wav')
        output_file_url = f'http://{params["ip_address"]}:{params["port_number"]}/audio/{output_file_name}_combined.wav'
        output_cache_url = f'http://{params["ip_address"]}:{params["port_number"]}/audiocache/{output_file_name}_combined.wav'
    try:
        sf.write(output_file_path, audio, samplerate=sample_rate)
        # Clean up unnecessary files
        for audio_file in audio_files:
            os.remove(audio_file)
    except Exception as e:
        # Handle exceptions (e.g., failed to write output file)
        return None, None
    return output_file_path, output_file_url, output_cache_url

# Generation API (separate from text-generation-webui)
@app.post("/api/tts-generate", response_class=JSONResponse)
async def tts_generate(
    text_input: str = Form(...),
    text_filtering: str = Form(...),
    character_voice_gen: str = Form(...),
    narrator_enabled: bool = Form(...),
    narrator_voice_gen: str = Form(...),
    text_not_inside: str = Form(...),
    language: str = Form(...),
    output_file_name: str = Form(...),
    output_file_timestamp: bool = Form(...),
    autoplay: bool = Form(...),
    autoplay_volume: float = Form(...),
    streaming: bool = Form(False),
):
    try:
        json_input_data = {
            "text_input": text_input,
            "text_filtering": text_filtering,
            "character_voice_gen": character_voice_gen,
            "narrator_enabled": narrator_enabled,
            "narrator_voice_gen": narrator_voice_gen,
            "text_not_inside": text_not_inside,
            "language": language,
            "output_file_name": output_file_name,
            "output_file_timestamp": output_file_timestamp,
            "autoplay": autoplay,
            "autoplay_volume": autoplay_volume,
            "streaming": streaming,
        }
        JSONresult = TTSGenerator.validate_json_input(json_input_data)
        if JSONresult is None:
            pass
        else:
            return JSONResponse(content={"error": JSONresult}, status_code=400)
        if narrator_enabled:
            processed_parts = process_text(text_input)
            audio_files_all_paragraphs = []
            for part_type, part in processed_parts:
                # Skip parts that are too short
                if len(part.strip()) <= 3:
                    continue
                # Determine the voice to use based on the part type
                if part_type == 'narrator':
                    voice_to_use = narrator_voice_gen
                    print(f"[{params['branding']}TTSGen] \033[92mNarrator\033[0m")  # Green
                elif part_type == 'character':
                    voice_to_use = character_voice_gen
                    print(f"[{params['branding']}TTSGen] \033[36mCharacter\033[0m")  # Yellow
                else:
                    # Handle ambiguous parts based on user preference
                    voice_to_use = character_voice_gen if text_not_inside == "character" else narrator_voice_gen
                    voice_description = "\033[36mCharacter (Text-not-inside)\033[0m" if text_not_inside == "character" else "\033[92mNarrator (Text-not-inside)\033[0m"
                    print(f"[{params['branding']}TTSGen] {voice_description}")
                # Replace multiple exclamation marks, question marks, or other punctuation with a single instance
                cleaned_part = re.sub(r'([!?.])\1+', r'\1', part)
                # Further clean to remove any other unwanted characters
                cleaned_part = re.sub(r'[^a-zA-Z0-9\s\.,;:!?\-\'"\u0400-\u04FFÀ-ÿ\u0150\u0151\u0170\u0171]\$', '', cleaned_part)
                # Remove all newline characters (single or multiple)
                cleaned_part = re.sub(r'\n+', ' ', cleaned_part)
                output_file = this_dir / "outputs" / f"{output_file_name}_{uuid.uuid4()}_{int(time.time())}.wav"
                output_file_str = output_file.as_posix()
                response = await generate_audio(cleaned_part, voice_to_use, language,temperature, repetition_penalty, output_file_str, streaming)
                audio_path = output_file_str
                audio_files_all_paragraphs.append(audio_path)
            # Combine audio files across paragraphs
            output_file_path, output_file_url, output_cache_url = combine(output_file_timestamp, output_file_name, audio_files_all_paragraphs)
        else:
            if output_file_timestamp:
                timestamp = int(time.time())
                # Generate a standard UUID
                original_uuid = uuid.uuid4()
                # Hash the UUID using SHA-256
                hash_object = hashlib.sha256(str(original_uuid).encode())
                hashed_uuid = hash_object.hexdigest()
                # Truncate to the desired length, for example, 16 characters
                short_uuid = hashed_uuid[:5]
                output_file_path = this_dir / "outputs" / f"{output_file_name}_{timestamp}{short_uuid}.wav"
                output_file_url = f'http://{params["ip_address"]}:{params["port_number"]}/audio/{output_file_name}_{timestamp}{short_uuid}.wav'
                output_cache_url = f'http://{params["ip_address"]}:{params["port_number"]}/audiocache/{output_file_name}_{timestamp}{short_uuid}.wav'
            else:
                output_file_path = this_dir / "outputs" / f"{output_file_name}.wav"
                output_file_url = f'http://{params["ip_address"]}:{params["port_number"]}/audio/{output_file_name}.wav'
                output_cache_url = f'http://{params["ip_address"]}:{params["port_number"]}/audiocache/{output_file_name}.wav'
            if text_filtering == "html":
                cleaned_string = html.unescape(standard_filtering(text_input))
                cleaned_string = re.sub(r'([!?.])\1+', r'\1', text_input)
                # Further clean to remove any other unwanted characters
                cleaned_string = re.sub(r'[^a-zA-Z0-9\s\.,;:!?\-\'"\u0400-\u04FFÀ-ÿ\u0150\u0151\u0170\u0171]\$', '', cleaned_string)
                # Remove all newline characters (single or multiple)
                cleaned_string = re.sub(r'\n+', ' ', cleaned_string)
            elif text_filtering == "standard":
                cleaned_string = re.sub(r'([!?.])\1+', r'\1', text_input)
                # Further clean to remove any other unwanted characters
                cleaned_string = re.sub(r'[^a-zA-Z0-9\s\.,;:!?\-\'"\u0400-\u04FFÀ-ÿ\u0150\u0151\u0170\u0171]\$', '', cleaned_string)
                # Remove all newline characters (single or multiple)
                cleaned_string = re.sub(r'\n+', ' ', cleaned_string)
            else:
                cleaned_string = text_input
            response = await generate_audio(cleaned_string, character_voice_gen, language, temperature, repetition_penalty, output_file_path, streaming)
        if sounddevice_installed == False or streaming == True:
            autoplay = False
        if autoplay:
            play_audio(output_file_path, autoplay_volume)       
        if streaming:
            return StreamingResponse(response, media_type="audio/wav")
        return JSONResponse(content={"status": "generate-success", "output_file_path": str(output_file_path), "output_file_url": str(output_file_url), "output_cache_url": str(output_cache_url)}, status_code=200)
    except Exception as e:
        return JSONResponse(content={"status": "generate-failure", "error": "An error occurred"}, status_code=500)


##########################
#### Current Settings ####
##########################
# Define the available models
models_available = [
    {"name": "Coqui", "model_name": "API TTS"},
    {"name": "Coqui", "model_name": "API Local"},
    {"name": "Coqui", "model_name": "XTTSv2 Local"}
]

@app.get('/api/currentsettings')
def get_current_settings():
    # Determine the current model loaded
    if params["tts_method_api_tts"]:
        current_model_loaded = "API TTS"
    elif params["tts_method_api_local"]:
        current_model_loaded = "API Local"
    elif params["tts_method_xtts_local"]:
        current_model_loaded = "XTTSv2 Local"
    else:
        current_model_loaded = None  # or a default value if no method is active

    settings = {
        "models_available": models_available,
        "current_model_loaded": current_model_loaded,
        "deepspeed_available": deepspeed_available,
        "deepspeed_status": params["deepspeed_activate"],
        "low_vram_status": params["low_vram"],
        "finetuned_model": finetuned_model
    }
    return settings  # Automatically converted to JSON by Fas

#############################
#### Word Add-in Sharing ####
#############################
# Mount the static files from the 'word_addin' directory
app.mount("/api/word_addin", StaticFiles(directory=os.path.join(this_dir / 'templates' / 'word_addin')), name="word_addin")

###################################################
#### Webserver Startup & Initial model Loading ####
###################################################

# Get the admin interface template
template = templates.get_template("admin.html")
# Render the template with the dynamic values
rendered_html = template.render(params=params)

###############################
#### Internal script ready ####
###############################
@app.get("/ready")
async def ready():
    return Response("Ready endpoint")

############################
#### External API ready ####
############################
@app.get("/api/ready")
async def ready():
    return Response("Ready")

@app.get("/")
async def read_root():
    return HTMLResponse(content=rendered_html, status_code=200)

@app.post("/api/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    start_time = time.time()  # 开始时间
    try:
        # 保存上传的文件到临时文件中
        temp_file_path = f"temp_{file.filename}"
        with open(temp_file_path, 'wb') as temp_file:
            content = await file.read()
            temp_file.write(content)
        
        # 使用Whisper模型转换音频为文字
        result = STT_model.transcribe(temp_file_path)
        text = result["text"]
        
        # 清理临时文件
        #await asyncio.sleep(1)  # 确保文件已关闭
        os.remove(temp_file_path)
        
        end_time = time.time()  # 结束时间
        processing_time = end_time - start_time  # 计算处理时间
        
        return {"text": text, "processing_time": processing_time}
    except Exception as e:
        return JSONResponse(status_code=500, content={"message": str(e), "processing_time": "N/A"})

# Start Uvicorn Webserver
host_parameter = params["ip_address"]
port_parameter = int(params["port_number"])

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=host_parameter, port=port_parameter, log_level="warning")
