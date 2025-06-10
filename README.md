# 🚀 ComfyuiGW: ComfyUI API Gateway

[![Project Status](https://img.shields.io/badge/status-active-success.svg)]()
[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue.svg)]()
[![ComfyUI Compatible](https://img.shields.io/badge/ComfyUI-compatible-brightgreen.svg)](https://github.com/comfyanonymous/ComfyUI)

**ComfyuiGW** is a powerful RESTful API Gateway designed specifically for running [ComfyUI version of Stable Diffusion Sketch](https://github.com/jordenyt/stable_diffusion_sketch/tree/comfyui). It provides a robust interface for executing complex workflows through both API endpoints and a user-friendly web interface.

## ✨ Key Features
- **REST API Gateway** - Execute ComfyUI workflows via simple HTTP requests
- **Web Interface** - User-friendly form-based workflow execution (`/runflow`)
- **Real-time Progress Tracking** - Monitor workflow execution with live updates
- **File Management** - Automatic handling of images, videos, and audio files
- **Workflow Configuration** - Simple JSON-based workflow definition
- **Preset Management** - Create reusable configurations with json files
- **Cross-platform Support** - Works on Windows and Linux systems

## 🛠️ Installation

### Windows Setup
1. Install [ComfyUI](https://github.com/comfyanonymous/ComfyUI)
2. Install required custom nodes for your workflows, e.g.:
   ```bash
   cd ComfyUI/custom_nodes
   git clone https://github.com/ltdrdata/ComfyUI-Manager
   git clone https://github.com/kijai/ComfyUI-Florence2
   git clone https://github.com/pythongosssss/ComfyUI-Custom-Scripts
   git clone https://github.com/kijai/ComfyUI-KJNodes
   git clone https://github.com/WASasquatch/was-node-suite-comfyui
   git clone https://github.com/BlenderNeko/ComfyUI_ADV_CLIP_emb
   git clone https://github.com/jamesWalker55/comfyui-various
   git clone https://github.com/Fannovel16/comfyui_controlnet_aux
   ```
3. Clone this repository:
   ```bash
   git clone https://github.com/your-repo/ComfyuiGW
   ```
4. Update `COMFYUI_PATH` in **run.bat** (Windows) or **run.sh** (Linux)
5. Start the server:
   ```bash
   run.bat  # Windows
   ./run.sh # Linux
   ```

## 📦 Adding a Workflow
1. Create or download a ComfyUI workflow JSON file
2. Place it in a subdirectory under `workflows/` (e.g., `workflows/my_workflow/`)
3. Create `workflows.config.json`:
   ```json
   { 
    "workflow_name_api": {
      "prompt_workflow": "your_workflow.json",
      "fields": {
        "param1": "15.inputs.param1",
        "param2": ["16.inputs.input1", "16.inputs.input2"]
      },
      "inputs": {
        "param1": {"type": "text", "default": "value"},
        "param2": {"type": "integer", "default": 10}
      }
    }
   }
   ```
4. Create `mode.config.json` for preset configurations (in [Stable Diffusion Sketch](https://github.com/jordenyt/stable_diffusion_sketch/tree/comfyui) app):
   ```json
   [
     {
      "name": "ModeName",
      "title": "Display Name",
      "showT2I": true,
      "show": true,
      "configurable": true,
      "default": {"param1": "value"},
      "fields": {
        "workflow": "workflow_name",
        "param1": "fixed_value"
      }
    }
   ]
   ```

## 🌐 Using the Web Interface
Access `http://localhost:5000/runflow` to execute workflows through a convenient form-based interface:

1. **Select Workflow** - Choose from available workflows
2. **Fill Parameters** - Provide inputs via text fields and file uploads
3. **Submit & Monitor** - Track real-time progress with step-by-step updates
4. **Retrieve Results** - Find output in `ComfyUI/output` directory


## 🧩 Project Structure
```
ComfyuiGW/
├── comfyui_server.py            # Main server implementation
├── run.bat                      # Windows launch script
├── run.sh                       # Linux launch script
├── requirements.txt             # Python dependencies
├── workflows/                   # Workflow configurations
│   ├── workflows.config.json    # Workflow form template
│   ├── mode.config.json         # Workflow list template
│   └── my_ComfyUI_workflow.json # Your custom ComfyUI workfiles API json
├── inputs/                      # Uploaded input files
├── templates/                   # Web interface templates
│   ├── runflow_form.html        # Workflow form template
│   ├── runflow_list.html        # Workflow list template
│   └── workflow_progress.html   # Progress tracking
└── mode_config.json             # Global mode configurations
```

## 📚 Configuration Files

### workflows.config.json
Defines workflow mappings and API parameters with the following structure:

```json
{
  "workflow_name": {
    "prompt_workflow": "filename.json",
    "fields": {
      "param1": "16.inputs.param1",
      "param2": ["15.inputs.input1", "15.inputs.input2"]
    },
    "inputs": {
      "param1": {"type": "text", "default": "default value"},
      "param2": {"type": "integer", "default": 10}
    }
  }
}
```

#### Parameter Descriptions:
- **workflow_name**: Unique identifier for the workflow (used in API calls)
- **prompt_workflow**: Path to the ComfyUI workflow JSON file
- **fields**: Maps API parameters to workflow node inputs:
  - Key: API parameter name
  - Value: Target node input(s) in format `"node_id.inputs.param_name"` (string for single input, array for multiple inputs)
- **descriptions**: (Optional) Description to be show in `runflow` web interface:
- **inputs**: (Optional) Defines `runflow` parameters:
  - **type**: Data type (text, string, integer, float, array, image, video, audio)
  - **default**: Default value if not provided

### mode.config.json
Defines preset configurations for workflows:

```json
{
  "name": "ModeName",
  "title": "Display Name",
  "showT2I": true,
  "show": true,
  "configurable": true,
  "default": {"param1": "value", "param2": 0.5},
  "fields": {
    "workflow": "workflow_name",
    "param1": "fixed_value",
    "param2": "$variable"
  }
}
```

#### Parameter Descriptions:
- **name**: Internal mode identifier (alphanumeric, no spaces)
- **title**: Display name shown in UI
- **showT2I**: Show in text-to-image section (true/false)
- **show**: Enable/disable mode visibility (true/false)
- **configurable**: Allow parameter customization in UI (true/false)
- **default**: Default values for configurable parameters
- **fields**: Fixed parameters passed to workflow:
  - **workflow**: Required - references workflow_name from workflows.config.json
  - Additional fields: Fixed values or variables (prefixed with $, filled by app) to pass to workflow



## ⚙️ System Requirements
- Python 3.8+
- ComfyUI installation
- Custom nodes (listed in Installation)

## ⁉️ Support
For issues and feature requests, please [open an issue](https://github.com/your-repo/ComfyuiGW/issues) on GitHub.

---

**ComfyuiGW** simplifies ComfyUI workflow execution through a well-designed API gateway and web interface, making complex image and video processing workflows accessible to both developers and end-users.
