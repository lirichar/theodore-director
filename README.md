# Theodore Director

Theodore Director 是一个独立于 ComfyUI 原生界面的移动端导演台。它通过 ComfyUI HTTP API 调用本机工作流，为 MiniMax H3 视频生成、Qwen Image Edit 图片编辑、素材管理、结果管理、任务队列和多账号访问提供更适合手机使用的前端。

ComfyUI 仍由原有启动器负责启动，默认运行在 `127.0.0.1:8188`；导演台作为独立服务运行在 `8190`，不会替代或修改 ComfyUI 的启动方式。

## 功能概览

- 文生视频、图生视频、参考生视频
- Qwen Image Edit 基础图片编辑
- 分镜导演
- 图片、视频、音频素材库
- 生成结果分组、预览、下载、删除、重新生成和导入素材
- 任务队列、任务归属、排队位置和预计等待时间
- 登录、邀请码注册和管理员账号管理
- 普通用户每日配额
- 管理员创建不限额公共账号
- Tailscale Funnel 公网 HTTPS 访问
- 独立的多页使用说明

## 目录结构

```text
导演台拓展/
├─ README.md
├─ 使用说明.md
├─ 启动独立手机导演台.bat
└─ mobile_director/
   ├─ server.py              # HTTP 服务、ComfyUI 对接、账号与文件 API
   ├─ auth.db                # 本地账号数据库，不应提交到 GitHub
   ├─ jobs.json              # 任务参数与结果分组元数据
   ├─ .thumbs/               # 图片和视频缩略图缓存
   └─ web/
      ├─ index.html          # 导演台主页面
      ├─ app.js              # 生成、素材、结果和队列交互
      ├─ account.html/js     # 账号中心与管理员后台
      ├─ login.html          # 登录和邀请码注册
      ├─ help*.html          # 多页使用说明
      └─ help-media/         # 使用说明中的图片与视频
```

## 核心：工作流与前端如何对接

### 1. 使用 ComfyUI API 工作流

导演台不在浏览器里重新实现采样、模型加载或视频编码。真正的生成过程仍由 ComfyUI 工作流完成。

首先在 ComfyUI 中确认工作流可以正常运行，然后将工作流导出为 API 格式 JSON。发布版默认从项目的 `workflows/` 目录读取：

```text
workflows/minimax_h3_t2v_api.json
workflows/minimax_h3_i2v_api.json
workflows/minimax_h3_r2v_api.json
workflows/qwen_image_edit_2511_api.json
```

对应关系：

| 前端模式 | API 工作流 |
|---|---|
| 文生视频 | `minimax_h3_t2v_api.json` |
| 图生视频 | `minimax_h3_i2v_api.json` |
| 参考生视频 | `minimax_h3_r2v_api.json` |
| 图片编辑 | `qwen_image_edit_2511_api.json` |

可以通过环境变量 `WORKFLOW_DIR` 覆盖工作流目录。工作流 JSON 默认不提交，以免意外发布本机配置或许可证不明确的内容。

### 2. 前端收集生成参数

`web/app.js` 将页面中的输入整理成 JSON 请求。例如视频生成请求：

```json
{
  "mode": "i2v",
  "title": "人物转身",
  "duration": 5,
  "aspect": "9:16 (Portrait Widescreen)",
  "resolution": 0.4,
  "count": 1,
  "prompt": "人物自然转身并看向镜头",
  "negative": "",
  "selected": ["character.png"],
  "firstFrame": "character.png",
  "lastFrame": ""
}
```

前端将其发送到：

```http
POST /api/generate
Content-Type: application/json
X-CSRF-Token: ...
```

图片编辑使用 `POST /api/qwen-generate`。

### 3. 后端加载并参数化工作流

`server.py` 中的 `workflow(req, user)` 负责 MiniMax H3，`qwen_workflow(req, user)` 负责 Qwen Image Edit。

处理过程如下：

```text
前端表单
   ↓ JSON
/api/generate 或 /api/qwen-generate
   ↓
读取对应的 API 工作流 JSON
   ↓
替换提示词、模型、种子、时长、比例、分辨率、素材路径和输出路径
   ↓
POST http://127.0.0.1:8188/prompt
   ↓
ComfyUI 校验并进入执行队列
```

后端不是依赖节点在图中的显示名称，而是根据 `class_type` 或已知节点 ID 找到目标节点。例如：

- `RandomNoise`：写入随机种子
- `PrimitiveFloat`：写入视频时长
- `ResolutionSelector`：写入比例、像素量和倍数
- `LoadImage`：写入当前账号素材目录中的图片
- `LoadVideo`：写入参考视频
- `MiniMaxH3ImageToVideo`：写入最终提示词和首尾帧
- `MiniMaxH3ReferenceToVideo`：写入参考图片、视频和音频
- `SaveVideo` / `SaveImageAdvanced`：写入账号隔离后的结果路径

### 4. 素材引用转换

前端插入的引用格式为：

```text
{{ref:素材文件名}}
```

参考生视频在提交前根据选中素材的类型和顺序将其转换为模型工作流认识的引用：

```text
{{ref:人物参考图}}   → <Picture 1>
{{ref:动作参考视频}} → <Video 1>
```

同时，后端动态创建 `LoadImage`、`LoadVideo` 和 `GetVideoComponents` 节点，并把节点输出连接到 MiniMax H3 参考输入。

Qwen 图片编辑采用相同思路，将图片映射为 `<Picture 1>`、`<Picture 2>`，并动态连接到正向和负向条件节点。

因此，新增模式时通常需要同时完成三件事：

1. 准备一个在 ComfyUI 中验证通过的 API 工作流。
2. 定义前端要提交的参数结构。
3. 在后端写一个工作流适配器，将前端参数映射到具体节点。

### 5. 提交、队列和结果关联

参数化后的工作流通过 ComfyUI `/prompt` 接口提交。返回的 `prompt_id` 用于识别任务。

导演台还会把本次请求参数写入 `jobs.json`，键为输出前缀。生成完成后扫描结果目录时，可根据文件路径找到原始参数，从而实现：

- 同参数结果自动分组
- 沿用原参数重新生成
- 显示结果所属项目
- 判断任务所有者

## 素材和结果管理

### 素材库

素材存放在 ComfyUI `input` 目录下，但按账号类型分开：

```text
管理员：input/theodore_mobile/
普通用户：input/theodore_users/u<用户ID>/
公共账号：input/theodore_public/u<用户ID>/
```

同一个公共账号可以由多人使用，因此登录该账号的人共享该账号内素材；不同公共账号之间相互隔离。

### 结果库

结果路径同样按账号隔离：

```text
管理员：output/TheodoreMobile/
普通用户：output/TheodoreUsers/u<用户ID>/
公共账号：output/TheodorePublic/u<用户ID>/
```

后端对路径进行 `resolve()` 和父目录检查，避免使用相对路径访问其他账号文件。

### 缩略图和媒体加载

- 图片缩略图使用 Pillow 转为 WebP。
- 视频缩略图使用 FFmpeg 抽取画面。
- 缩略图缓存在 `mobile_director/.thumbs/`。
- 视频默认只加载缩略图，点击后再加载媒体文件。
- 媒体接口支持 HTTP Range，方便视频拖动和分段加载。

## 账号与权限实现

账号数据存储在本地 SQLite 数据库 `auth.db`。

### 密码和会话

- 密码使用 `hashlib.scrypt` 加随机盐保存。
- 登录成功后生成随机会话令牌。
- 数据库只保存令牌的 SHA-256 摘要。
- Cookie 使用 `HttpOnly` 和 `SameSite=Strict`。
- HTTPS 代理下可使用 `Secure` Cookie。
- 修改类请求需要 `X-CSRF-Token`。
- 登录失败按 IP 进行简单限速。

### 账号类型

| 类型 | 创建方式 | 配额 | 数据范围 |
|---|---|---|---|
| 管理员 | 首次初始化 | 不限 | 管理员数据与后台管理 |
| 普通用户 | 邀请码注册 | 每日 5 视频、10 图片 | 独立目录 |
| 公共账号 | 管理员直接创建 | 不限 | 同账号共享，不同公共账号隔离 |

管理员可以：

- 创建邀请码
- 创建公共账号
- 查看账号列表
- 重置普通用户当天额度
- 重置账号密码

## 任务队列和预计等待时间

导演台读取 ComfyUI `/queue` 接口，将工作流输出前缀与 `jobs.json` 元数据关联，显示：

- 运行中和等待中任务数量
- 任务所有者
- “我的任务”标记
- 前方任务数量
- 预计等待时间
- 取消任务

目前预计时间采用经验值估算，不是 ComfyUI 的精确剩余时间。运行中任务通过 `/interrupt` 取消，等待中任务通过 `/queue` 删除。

## 使用说明实现

使用说明采用独立 HTML 页面，不受 Markdown 限制，因此可以直接嵌入：

```html
<img src="/help-media/example.png" alt="操作截图">
<video src="/help-media/example.mp4" controls playsinline></video>
```

当前页面包括总览、图生视频案例、参考生视频案例、图片编辑案例和分镜导演案例。

## 本地启动

### 前置条件

1. ComfyUI 已安装并能正常运行。
2. MiniMax H3、Qwen Image Edit 及工作流依赖节点已安装。
3. API 工作流 JSON 已导出。
4. Python 环境包含 Pillow。
5. FFmpeg 路径可用。

### 启动顺序

1. 使用原启动器启动 ComfyUI，确认 `http://127.0.0.1:8188/` 可访问。
2. 运行 `启动独立手机导演台.bat`。
3. 打开 `http://127.0.0.1:8190/`。

## 公网访问

当前方案使用 Tailscale Funnel：

```powershell
tailscale funnel --bg 8190
```

关闭公网入口：

```powershell
tailscale funnel --https=443 off
```

公网发布前必须先创建管理员账号并验证未登录接口返回 `401`。不要直接将 ComfyUI 的 `8188` 端口暴露到公网。

## 未来优化方向

### 工作流适配

- 将节点 ID 和模型文件名移出源码，改为可配置适配器。
- 为每个工作流增加版本、必需节点和输入字段声明。
- 启动时自动检查缺失模型、节点和工作流。
- 提供工作流映射页面，允许管理员在前端选择节点输入。
- 支持更多视频与图片模型，而不是把接口绑定在 MiniMax/Qwen 上。

### 队列与预计时间

- 使用历史任务实际耗时训练更准确的预计时间模型。
- 区分图片、文生视频、图生视频和参考生视频的平均耗时。
- 保存任务状态和失败原因，而不是只读取当前 ComfyUI 队列。
- 增加任务优先级、公平排队和管理员调度。

### 数据和账号

- 将 `jobs.json` 迁移到 SQLite，避免并发写入冲突。
- 增加邀请码列表、停用账号、删除账号和操作审计。
- 支持用户自行修改密码和管理员强制登出。
- 增加磁盘配额、文件数量限制和垃圾清理策略。
- 为公共账号增加显示名称、备注和成员使用记录。

### 安全

- 使用成熟 Web 框架和认证库。
- 增加严格的 Content Security Policy、安全响应头和上传文件验证。
- 对登录、注册、生成和上传分别限速。
- 生产环境只允许 HTTPS，并强制 `Secure` Cookie。
- 增加数据库备份、恢复和密钥轮换方案。
- 对管理员操作记录审计日志。

### 前端体验

- 使用组件化前端框架重构当前单文件 JavaScript。
- 加入 WebSocket/SSE，实时推送队列和生成结果。
- 增加上传进度、断点续传和移动端视频压缩。
- 支持素材标签、搜索、收藏和项目归档。
- 在结果库中提供批量操作和更完整的参数查看。
- 让使用说明中的示例媒体由后台直接管理。

### 部署

- 提供首次启动向导，自动配置 ComfyUI、工作流目录和 FFmpeg。
- 提供 Docker 方案，同时保留 Windows 本地部署方式。
- 增加健康检查、日志轮转和自动更新。
- 将 Tailscale、Cloudflare Tunnel 等公网方案做成可选部署文档。

## 当前技术定位

当前版本适合个人工作站、小团队内测和共享 GPU 主机的轻量入口。它已经打通前端、工作流、ComfyUI 队列、素材和多账号数据隔离，但在正式公开发布前仍建议完成配置解耦、数据库任务化、审计、安全头和自动化测试。

## License

发布到 GitHub 前请根据所使用的 ComfyUI 节点、模型、工作流模板和示例媒体许可证选择合适的开源许可证。模型权重通常有独立许可，不应直接包含在本仓库中。
