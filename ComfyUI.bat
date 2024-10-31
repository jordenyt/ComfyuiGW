set COMFYUIPATH=E:\Workspace\ComfyUI
set ANACONDA_PATH=C:\Users\Jorden\anaconda3

cd /d %COMFYUIPATH%
call %ANACONDA_PATH%\condabin\activate
call %COMFYUIPATH%\venv\Scripts\activate
python main.py --fast
