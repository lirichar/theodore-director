# Theodore Director

面向短剧制作的本地导演台。可独立进行图片、视频和文本创作，也可以在一个项目中管理剧本、分镜、参考素材和生成版本。

- 工作台：视频生成与人物替换、图片生成与 Qwen 图片编辑、剧本与分镜。
- 素材库：标签、检索、多选整理、跨项目引用及回收站。项目引用不复制原文件。
- 任务与版本：查看生成状态和失败原因，沿用原参数调整后重试，读取旧任务历史。
- 支持配置自己的 OpenAI 兼容 API；本地工作流通过 ComfyUI API 执行。

## 启动

需要 Python 3.10+。本地工作流还需要运行中的 ComfyUI、对应节点和模型。视频缩略图需要 FFmpeg。

```powershell
python -m pip install -r requirements.txt
$env:THEODORE_COMFY_ROOT = 'D:\path\to\ComfyUI'
python mobile_director/server.py
```

打开 [导演台](http://127.0.0.1:8190/)，首次启动时创建管理员账号。也可编辑并运行 `start.example.bat`。默认 ComfyUI 地址为 `http://127.0.0.1:8188`，导演台端口为 `8190`。配置项见 [.env.example](.env.example)；请将所需值设置为环境变量，服务不自动读取该示例文件。

在“API 与账号设置”中填写自己的 API 地址、密钥和模型。使用本地生成时，按[工作流配置说明](workflows/README.md)准备兼容的 API 图。工作流、模型、账号数据库、任务历史和媒体文件均需在本机配置或生成。

日常操作见[使用说明](使用说明.md)。

## 验证

以下测试使用临时数据库和模拟工作流，不调用真实模型。前端测试需要 Node.js。

```powershell
cd mobile_director
python -m unittest discover -s tests -v
node --test tests/frontend_contract.test.cjs
```
