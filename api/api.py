import glob
import json
import sys
import time

from fastapi import FastAPI, File, UploadFile, Request, Query
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, JSONResponse
from typing import Optional
import os
import pathlib
from utils.image import convert_png_to_mask, mask_invert, remove_bg, decode_base64_to_image, encode_to_base64
import utils.datadir as datadir
# from utils.req import interrogate
from utils.utils import project_dir
from PIL import Image
import scripts.interrogate
from utils.pt_logging import ia_logging, log_echo
import json
# from .models import *

from utils.constant import mode_params, self_innovate_mode, init_model, api_queue_dir
from scripts.inpaint import Inpainting
from scripts.piplines.controlnet_pre import lineart_image
from utils.cmd_args import opts as shared
from .call_queue import LocalFileQueue as Queue

interrogate = scripts.interrogate.InterrogateModels()

gpipe = None


def set_model():
    global gpipe
    gpipe = Inpainting(
        base_model=init_model['base_mode'],
        subfolder=None,
        controlnet=init_model['controlnets'],
        textual_inversion=init_model['textual_inversion'],
    )
    return gpipe


if shared.setup_mode:
    set_model()


def commodity_image_generate_api_params(request_data, id_task=None):
    if len(request_data['input_images']) < 1 or request_data['mask_image'] == '':
        if id_task is not None:
            input_image = Image.open(
                datadir.api_generate_commodity_dir.format(id_task=id_task, type="input", ) + '/0.png').convert("RGB")
            mask = Image.open(
                datadir.api_generate_commodity_dir.format(id_task=id_task, type="input", ) + '/1.png').convert("RGB")
    else:

        input_image = request_data['input_images'][0]
        mask = request_data['mask_image']

    base_model = request_data['checkpoint_addr']
    pos_prompt = request_data['preset'][0]['param']['prompt']
    neg_prompt = request_data['preset'][0]['param']['negative_prompt']
    batch_count = request_data['preset'][0]['count']
    sampler_name = request_data['preset'][0]['param']['sampler']
    width = request_data['preset'][0]['param']['width']
    height = request_data['preset'][0]['param']['height']
    contr_inp_weight = mode_params[self_innovate_mode]['inpaint_weight']
    contr_ipa_weight = mode_params[self_innovate_mode]['ip-adapter_weight']
    contr_lin_weight = mode_params[self_innovate_mode]['lineart_weight']
    if len(request_data['preset'][0]['param']['controlnets']) > 0:
        for controlnet in request_data['preset'][0]['param']['controlnets']:
            if controlnet['controlnet_module'] == 'inpaint_only+lama':
                contr_inp_weight = controlnet['weight']
            elif controlnet['controlnet_module'] == 'ip-adapter_clip_sd15':
                contr_ipa_weight = controlnet['weight']
            elif controlnet['controlnet_module'] == 'lineart_realistic':
                contr_lin_weight = controlnet['weight']

    return input_image, mask, base_model, pos_prompt, neg_prompt, batch_count, sampler_name, contr_inp_weight, contr_ipa_weight, contr_lin_weight, width, height


queue = Queue(api_queue_dir)


def saveimage(id_task, _type: str, images: list):
    '''
    :param id_task:
    :param _type: enumerate:input,output
    :param images:
    :return:
    '''
    from utils.constant import PT_ENV
    if PT_ENV is None:
        return None

    img_dir = datadir.api_generate_commodity_dir.format(id_task=id_task, type=_type)
    try:
        pathlib.Path(img_dir).mkdir(parents=True, exist_ok=True)
        idx = len(os.listdir(img_dir))
        for im in images:
            if type(im) is not Image.Image:
                decode_base64_to_image(im).save(f"{img_dir}/{idx}.png")
            else:
                im.save(f"{img_dir}/{idx}.png")
            idx = idx + 1
    except Exception as e:
        log_echo("API Error", msg={"id_task": id_task}, exception=e, is_collect=True, )


def print_log(request: Request, **kwargs):
    import copy
    tmp = copy.deepcopy(kwargs)
    tmp['data']['data']['input_images'][0] = kwargs['data']['data']['input_images'][0][:100]
    tmp['data']['data']['mask_image'] = kwargs['data']['data']['mask_image'][:100]
    tmp = json.dumps(tmp)
    log_echo("API Params", msg={
        "api": request.url.path,
        "client_host": request.client.host,
        "host": request.headers['host'],
        "req_params": str(request.query_params),
    }, level='info', path=request.url.path)
    return tmp


def call_queue_task():
    while 1:
        data = None
        try:
            origin_data = queue.dequeue(is_complete=False)
            if origin_data is not None:
                log_echo("Call Queue Task: ", msg=origin_data, level='info', path='call_queue_task')
                data = json.loads(origin_data)
                input_image, mask, base_model, pos_prompt, neg_prompt, batch_count, sampler_name, contr_inp_weight, contr_ipa_weight, contr_lin_weight, width, height \
                    = commodity_image_generate_api_params(data['data'], id_task=data['id_task'])
                if type(input_image) is str:
                    input_image = decode_base64_to_image(input_image)
                if type(mask) is str:
                    mask = decode_base64_to_image(mask)

                pos_prompt = pos_prompt % interrogate.interrogate(input_image) if '%s' in pos_prompt else pos_prompt

                if gpipe is None:
                    pipe = set_model()
                else:
                    pipe = gpipe

                lineart_input_img = lineart_image(input_image=input_image, width=width)
                saveimage(id_task=data['id_task'], _type="input", images=[lineart_input_img])
                pipe.set_controlnet_input([
                    {
                        'scale': contr_ipa_weight,
                        'image': input_image,
                    },
                    {
                        'scale': contr_lin_weight,
                        'image': lineart_input_img
                    }
                ])

                pipe.run_inpaint(
                    input_image=input_image,
                    mask_image=mask,
                    prompt=pos_prompt,
                    n_prompt=neg_prompt,
                    ddim_steps=30,
                    cfg_scale=7.5,
                    seed=-1,
                    composite_chk=True,
                    # sampler_name="UniPC",
                    sampler_name=sampler_name,
                    iteration_count=batch_count,
                    width=(int(width) // 8) * 8,
                    height=(int(height) // 8) * 8,
                    strength=contr_inp_weight,
                    eta=31337,
                    output=datadir.api_generate_commodity_dir.format(id_task=data['id_task'], type="output", ),
                    ret_base64=True
                )
                queue.complete(origin_data)
        except Exception as e:
            log_echo("API Call Queue Error", msg=data, exception=e, is_collect=True, path='call_queue_task')
        finally:
            time.sleep(0.5)


class Api:
    def __init__(self, app: FastAPI):
        self.app = app
        self.app.add_api_route("/iframe", self.read_html_file, methods=["get"], response_class=HTMLResponse)
        self.app.add_api_route("/iframe_clothes", self.read_clothes_html_file, methods=["get"],
                               response_class=HTMLResponse)
        self.app.add_api_route("/upload_image", self.upload_image, methods=["post"])
        self.app.add_api_route("/upload_clothes_image", self.upload_clothes_image, methods=["post"])
        self.app.add_api_route("/deft_scene", self.deft_scene, methods=["get"])
        self.app.add_api_route("/human_imag", self.human_imag, methods=["get"])
        self.app.add_api_route("/clothes_imag", self.clothes_imag, methods=["get"])

        self.app.add_api_route("/v1/image/interrogate", self.interrogate, methods=["post"])

        self.app.add_api_route("/v1/commodity_image/generate", self.commodity_image_generate, methods=["post"],
                               response_class=JSONResponse)
        self.app.add_api_route("/v1/commodity_image/reception", self.commodity_image_reception, methods=["post"],
                               response_class=JSONResponse)
        self.app.add_api_route("/v1/commodity_image/result", self.commodity_image_result, methods=["get"],
                               response_class=JSONResponse)

        # self.queue = Queue(api_queue_dir)

    def interrogate(self):
        pass

    async def commodity_image_reception(self, request: Request):
        strt_time = time.time()
        result = {
            "data": [],
            "message": "success",
            "duration": 0,
            "status": 200
        }
        data = await request.json()
        download_time = time.time() - strt_time
        if data['id_task'] is None or len(data['data']['input_images']) < 1 or data['data']['mask_image'] is None:
            result['message'] = "data is None"
            result['status'] = 400
            return result

        import copy
        tmp = copy.deepcopy(data)
        tmp['data']['input_images'][0] = tmp['data']['input_images'][0][:100]
        tmp['data']['mask_image'] = tmp['data']['mask_image'][:100]
        tmp = json.dumps(tmp)
        log_echo("API Params", msg={
            "api": request.url.path,
            "client_host": request.client.host,
            "host": request.headers['host'],
            "req_params": tmp,
            "download_time": download_time
        }, level='info', path=request.url.path)

        saveimage(
            id_task=data['id_task'],
            _type="input",
            images=[data['data']['input_images'][0], data['data']['mask_image']]
        )
        data['data']['input_images'] = []
        data['data']['mask_image'] = ''
        queue.enqueue(json.dumps(data))
        result['duration'] = time.time() - strt_time
        return result

    def commodity_image_result(self, request: Request):

        result = {
            "data": [],
            "message": "success",
            "duration": 0,
            "status": 200
        }
        if request.query_params['id_task'] is None:
            result['message'] = "id_task is None"
            result['status'] = 400
            return result
        log_echo("API Result", {
            "api": request.url.path,
            "client_host": request.client.host,
            "host": request.headers['host'],
            "req_params": str(request.query_params),
        }, level='info', path=request.url.path)
        try:
            if 'idx' not in request.query_params:
                img_list = glob.glob(datadir.api_generate_commodity_dir.format(id_task=request.query_params['id_task'],
                                                                               type="output", ) + "/*.png")
            else:
                img_list = glob.glob(datadir.api_generate_commodity_dir.format(id_task=request.query_params['id_task'],
                                                                               type="output", ) + f"/{int(request.query_params['idx'])}.png")

            for path in img_list:
                im = Image.open(path).convert("RGB")
                result['data'].append(encode_to_base64(im))
        except Exception as e:
            log_echo("API Error", {
                "api": request.url.path,
                "client_host": request.client.host,
                "host": request.headers['host'],
                "req_params": str(request.query_params),
            }, e, is_collect=True, path=request.url.path)
            result['message'] = str(e)
            result['message'] = 500

        return result

    async def commodity_image_generate(self, request: Request):

        strt_time = time.time()
        data = await request.json()
        download_time = time.time() - strt_time
        if data is None or data['data'] is None or data['id_task'] is None:
            return {"message": "data is None", "data": None, "duration": 0}

        import copy

        req_params = copy.deepcopy(data)
        req_params['data']['input_images'][0] = req_params['data']['input_images'][0][:100]
        req_params['data']['mask_image'] = req_params['data']['mask_image'][:100]
        req_params = json.dumps(req_params)
        log_echo("API Params", msg={
            "api": request.url.path,
            "client_host": request.client.host,
            "host": request.headers['host'],
            "req_params": req_params,
            "download_time": download_time
        }, level='info', path=request.url.path)

        saveimage(
            id_task=data['id_task'],
            _type="input",
            images=[data['data']['input_images'][0], data['data']['mask_image']]
        )
        ret = []
        try:
            input_image, mask, base_model, pos_prompt, neg_prompt, batch_count, sampler_name, contr_inp_weight, contr_ipa_weight, contr_lin_weight, width, height \
                = commodity_image_generate_api_params(data.get('data'))

            input_image = decode_base64_to_image(input_image)
            mask = decode_base64_to_image(mask)

            pos_prompt = pos_prompt % interrogate.interrogate(input_image) if '%s' in pos_prompt else pos_prompt

            if gpipe is None:
                pipe = set_model()
            else:
                pipe = gpipe

            lineart_input_img = lineart_image(input_image=input_image, width=width)
            saveimage(id_task=data['id_task'], _type="input", images=[lineart_input_img])
            pipe.set_controlnet_input([
                {
                    'scale': contr_ipa_weight,
                    'image': input_image,
                },
                {
                    'scale': contr_lin_weight,
                    'image': lineart_input_img
                }
            ])

            ret = pipe.run_inpaint(
                input_image=input_image,
                mask_image=mask,
                prompt=pos_prompt,
                n_prompt=neg_prompt,
                ddim_steps=30,
                cfg_scale=7.5,
                seed=-1,
                composite_chk=True,
                # sampler_name="UniPC",
                sampler_name=sampler_name,
                iteration_count=batch_count,
                width=(int(width) // 8) * 8,
                height=(int(height) // 8) * 8,
                strength=contr_inp_weight,
                eta=31337,
                output=datadir.api_generate_commodity_dir.format(id_task=data['id_task'], type="output", ),
                ret_base64=True
            )
        except Exception as e:
            log_echo("API Error", {
                "api": request.url.path,
                "client_host": request.client.host,
                "host": request.headers['host'],
                "req_params": req_params,
            }, e, is_collect=True, path=request.url.path)

        end_time = time.time()
        duration = end_time - strt_time
        ia_logging.info(f"API Duration Time:{duration}")
        return {"data": ret, "duration": duration}

    def read_html_file(self):
        file_path = f'{project_dir}/view/editimg.html'
        with open(file_path, "r", encoding='utf-8') as file:
            html_content = file.read()
        return HTMLResponse(content=html_content)

    def read_clothes_html_file(self):
        file_path = f'{project_dir}/view/clothes_editimg.html'
        with open(file_path, "r", encoding='utf-8') as file:
            html_content = file.read()
        return HTMLResponse(content=html_content)

    def upload_image(self, file: UploadFile = File(...), img_type: Optional[int] = None):
        if not os.path.exists(datadir.commodity_merge_scene_image_dir):
            pathlib.Path(datadir.commodity_merge_scene_image_dir).mkdir(parents=True, exist_ok=True)
            pathlib.Path(datadir.merge_after_mask_image_dir).mkdir(parents=True, exist_ok=True)
            pathlib.Path(datadir.mask_image_dir).mkdir(parents=True, exist_ok=True)
            pathlib.Path(datadir.merge_after_mask_cut_image_dir).mkdir(parents=True, exist_ok=True)

        # 制作mask
        if img_type == 2:
            try:
                contents = file.file.read()
                after_mask_path = f'{datadir.merge_after_mask_image_dir}/{datadir.get_file_idx()}.png'
                mask_path = f'{datadir.mask_image_dir}/{datadir.get_file_idx()}.png'
                merge_after_mask_cut_image_dir = f'{datadir.merge_after_mask_cut_image_dir}/{datadir.get_file_idx()}.png'
                with open(after_mask_path, 'wb') as f:
                    f.write(contents)

                convert_png_to_mask(after_mask_path, mask_path)
                mask_invert(mask_path, merge_after_mask_cut_image_dir)
                # remove_bg(after_mask_path, mask_path, True, False)
                # mask_invert(mask_path, after_mask_path)

                # convert_png_to_mask(mask_path, after_mask_path)
            except Exception as e:
                error_message = str(e)
                return {"data": f"{mask_path}, type:{img_type}, error:{error_message}"}
            return {"data": f"{mask_path}, type:{img_type}"}
        # 场景图
        else:
            i_path = f'{datadir.commodity_merge_scene_image_dir}/{datadir.get_file_idx()}.png'
            contents = file.file.read()
            with open(i_path, 'wb') as f:
                f.write(contents)
            # return {"data": f"{i_path}, type{img_type}", "caption": interrogate(i_path)}
            img = Image.open(i_path)
            img = img.convert('RGB')

            return {"data": f"{i_path}, type{img_type}", "caption": interrogate.interrogate(img)}

    def upload_clothes_image(self, file: UploadFile = File(...), img_type: Optional[int] = None):
        if not os.path.exists(datadir.clothes_merge_scene_dir):
            pathlib.Path(datadir.clothes_merge_scene_dir).mkdir(parents=True, exist_ok=True)
            pathlib.Path(datadir.clothes_dir).mkdir(parents=True, exist_ok=True)
            pathlib.Path(datadir.clothes_mask_dir).mkdir(parents=True, exist_ok=True)

        # 制作mask
        if img_type == 2:
            try:
                contents = file.file.read()
                clothes_path = f'{datadir.clothes_dir}/{datadir.get_file_idx(check_dir=datadir.clothes_dir)}.png'
                mask_path = f'{datadir.clothes_mask_dir}/{datadir.get_file_idx(check_dir=datadir.clothes_dir)}.png'
                with open(clothes_path, 'wb') as f:
                    f.write(contents)

                convert_png_to_mask(clothes_path, mask_path)
            except Exception as e:
                error_message = str(e)
                return {"data": f"{mask_path}, type:{img_type}, error:{error_message}"}
            return {"data": f"{mask_path}, type:{img_type}"}
        # 场景图
        else:
            i_path = f'{datadir.clothes_merge_scene_dir}/{datadir.get_file_idx(check_dir=datadir.clothes_dir)}.png'
            contents = file.file.read()
            with open(i_path, 'wb') as f:
                f.write(contents)
            return {"data": f"{i_path}, type{img_type}", "caption": interrogate(i_path)}

    def deft_scene(self, size_type: Optional[int] = None):

        try:
            if size_type == 1:
                return FileResponse(f"{project_dir}/worker_data/template/768x1024.png")
            elif size_type == 2:
                return FileResponse(f"{project_dir}/worker_data/template/800x1422.jpeg")
            else:
                return FileResponse(f"{project_dir}/worker_data/template/800x800.png")
        except Exception as e:
            return {"message": f"There was an error reading the image:{str(e)}"}

    def human_imag(self):
        try:
            return FileResponse(f"{project_dir}/worker_data/template/human_image.png")
        except Exception as e:
            return {"message": f"There was an error reading the image:{str(e)}"}

    def clothes_imag(self):
        try:
            return FileResponse(f"{project_dir}/worker_data/template/clothes_image.png")
        except Exception as e:
            return {"message": f"There was an error reading the image:{str(e)}"}
