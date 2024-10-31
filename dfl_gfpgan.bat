@echo off 
set CUR_DIR=%CD%
set DFL_FOLDER=E:\Workspace\DeepFaceLab_2
set GFPGAN_FOLDER=E:\Workspace\GFPGAN
set ESRGAN_FOLDER=E:\Workspace\ESRGAN
set PIC_FOLDER=E:\Workspace\dflAPI
set ANACONDA_PATH=C:\Users\Jorden\anaconda3

call %DFL_FOLDER%\_internal\setenv.bat 

cd /d %PIC_FOLDER%
rmdir /s /q restored_imgs
rmdir /s /q aligned
rmdir /s /q merged_mask
rmdir /s /q merged

"%PYTHON_EXECUTABLE%" "%DFL_ROOT%\main.py" extract ^
    --input-dir "%PIC_FOLDER%" ^
    --output-dir "%PIC_FOLDER%\aligned" ^
    --detector s3fd ^
	--face-type whole_face ^
	--image-size 512 ^
	--jpeg-quality 90 ^
	--no-output-debug ^
	--force-gpu-idxs 0 ^
    --max-faces-from-image 0

"%PYTHON_EXECUTABLE%" "%DFL_ROOT%\main.py" merge ^
    --input-dir "%PIC_FOLDER%" ^
    --output-dir "%PIC_FOLDER%\restored_imgs" ^
    --output-mask-dir "%PIC_FOLDER%\merged_mask" ^
    --aligned-dir "%PIC_FOLDER%\aligned" ^
    --model-dir "%WORKSPACE%\model" ^
	--force-model-name %1 ^
	--silent-start ^
    --model SAEHD

rem call %ANACONDA_PATH%\condabin\activate gfpgan

rem cd /d %GFPGAN_FOLDER%
rem %ANACONDA_PATH%\envs\gfpgan\python inference_gfpgan.py -i %PIC_FOLDER%\merged -o %PIC_FOLDER% -v 1.4 -s 1 --bg_upsampler None

cd /d %PIC_FOLDER%
rem rmdir /s /q aligned
rem rmdir /s /q merged
rem rmdir /s /q merged_mask

rem conda deactivate