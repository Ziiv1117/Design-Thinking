# Deadline Keeper 启动说明

本文用于在新电脑上完成启动前配置并运行 Deadline Keeper。服务包含 FastAPI 后端、简单测试页面、本地 PaddleOCR、SQLite 和阿里云百炼 Qwen 模型调用。

## 1. 准备条件

- Git。
- Python 3.11 或以上，推荐 Python 3.12。
- Python 实际连接的 SQLite 3.51.3 或以上。
- 可用的阿里云百炼 API Key。
- 安装依赖和下载 OCR 模型时需要联网。OCR 模型准备完成后，OCR 本身不需要联网；任务提取和匹配仍需要联网调用百炼。
- 不需要 GPU，当前 OCR 使用 CPU。

以下命令默认从仓库根目录开始执行。

## 2. 获取代码并进入后端目录

```bash
git clone https://github.com/May10001/Design-Thinking.git
cd Design-Thinking/backend
```

如果已经获得仓库压缩包，解压后进入其中的 `backend` 目录即可。

## 3. 创建虚拟环境并安装依赖

### macOS / Linux

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[ocr,test]"
```

如果命令是 `python3.11` 或 `python`，替换第一行中的解释器名称即可，但必须满足版本要求。

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[ocr,test]"
```

检查 Python 和 SQLite：

```bash
python --version
python -c "import sqlite3; print(sqlite3.sqlite_version)"
```

如果 Python 低于 3.11 或 SQLite 低于 3.51.3，应先更换或升级 Python 环境，不要继续启动。

## 4. 准备本地 OCR 模型

最终目录结构必须为：

```text
backend/
└── models/
    └── paddleocr/
        ├── det/
        │   ├── inference.json
        │   ├── inference.pdiparams
        │   └── inference.yml
        └── rec/
            ├── inference.json
            ├── inference.pdiparams
            └── inference.yml
```

`models` 被 Git 忽略，克隆仓库后通常需要单独准备。可以复制其他已配置电脑上的整个 `models/paddleocr` 目录，也可以按下面方式下载。

### macOS / Linux 下载方式

确保当前位于 `backend` 目录：

```bash
mkdir -p models/paddleocr/det models/paddleocr/rec

curl -L --fail \
  https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv5_mobile_det_infer.tar \
  | tar -x -C models/paddleocr/det --strip-components=1

curl -L --fail \
  https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv5_mobile_rec_infer.tar \
  | tar -x -C models/paddleocr/rec --strip-components=1
```

### Windows PowerShell 下载方式

```powershell
New-Item -ItemType Directory -Force models/paddleocr/det, models/paddleocr/rec

curl.exe -L --fail "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv5_mobile_det_infer.tar" -o paddleocr-det.tar
curl.exe -L --fail "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv5_mobile_rec_infer.tar" -o paddleocr-rec.tar

tar -xf paddleocr-det.tar -C models/paddleocr/det --strip-components=1
tar -xf paddleocr-rec.tar -C models/paddleocr/rec --strip-components=1
```

下载和解压完成后，检查上述六个模型文件是否存在，尤其注意不要在 `det` 或 `rec` 外面多套一层目录。

## 5. 创建并填写 `.env`

### macOS / Linux

```bash
cp .env.example .env
```

### Windows PowerShell

```powershell
Copy-Item .env.example .env
```

编辑 `backend/.env`，填入以下内容：

```dotenv
DATABASE_URL=sqlite:///./data/deadline_keeper.db
DEFAULT_TIMEZONE=Asia/Shanghai
DEMO_USER_ID=00000000-0000-4000-8000-000000000001
DEMO_USER_NAME=演示用户

LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=填写自己的百炼API密钥
LLM_MODEL=qwen3.8-max
LLM_TIMEOUT_SECONDS=40

OCR_MODEL_DIR=./models/paddleocr
OCR_TIMEOUT_SECONDS=30
ANALYSIS_TIMEOUT_SECONDS=90
```

注意事项：

- `LLM_API_KEY` 不能留空，等号与密钥之间不要加空格。
- `LLM_BASE_URL` 填兼容模式基础地址，不要在末尾添加 `/chat/completions`。
- `OCR_MODEL_DIR` 和 `DATABASE_URL` 均相对于 `backend` 目录。
- `.env` 包含密钥，已被 Git 忽略，不要提交或公开分享。
- 模型调用会产生相应供应商的 API 用量或费用。

## 6. 初始化数据库

确保当前位于 `backend`，并且虚拟环境已经激活：

```bash
dotenv -f .env run -- python -m alembic upgrade head
```

`dotenv run` 会把 `.env` 注入当前启动命令，macOS、Linux 和 Windows 都可以使用，不需要手动执行 `source .env`。

数据库文件会创建在：

```text
backend/data/deadline_keeper.db
```

重复执行迁移命令是安全的，已是最新版本时不会重复建表。

## 7. 启动服务

```bash
dotenv -f .env run -- python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

首次启动需要加载 OCR，可能等待几十秒。看到以下输出表示启动完成：

```text
Application startup complete.
Uvicorn running on http://127.0.0.1:8000
```

启动后不要关闭该终端。

## 8. 验证启动结果

浏览器访问：

- 测试页面：`http://127.0.0.1:8000/`
- 接口文档：`http://127.0.0.1:8000/docs`
- 健康检查：`http://127.0.0.1:8000/health/ready`

健康检查应返回：

```json
{"status":"ready"}
```

测试页面支持文字通知、PNG/JPEG 截图、模型分析、草稿编辑、人工确认和任务列表。每次“导入并分析”都会真实调用 Qwen。

## 9. 运行测试

### 自动化回归测试

从仓库根目录执行，不消耗模型额度：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m pytest -q backend/tests
```

Windows PowerShell 可使用：

```powershell
$env:PYTHONPATH="backend"
backend\.venv\Scripts\python.exe -m pytest -q backend/tests
```

### 真实模型公告验收

从仓库根目录执行，会实际调用百炼：

```bash
PYTHONPATH=backend backend/.venv/bin/python -m backend.tests.live_notices --env-file backend/.env
```

Windows PowerShell 可先设置 `$env:PYTHONPATH="backend"`，再执行：

```powershell
backend\.venv\Scripts\python.exe -m backend.tests.live_notices --env-file backend/.env
```

## 10. 常见问题

| 现象 | 检查与处理 |
| --- | --- |
| `SQLite >= 3.51.3 required` | 检查虚拟环境中的 Python 和 SQLite 版本，不要只看系统另一个 Python。 |
| `OCR_MODEL_DIR must contain local det/ and rec/` | 确认当前从 `backend` 启动，并检查 `models/paddleocr/det`、`rec` 及模型文件。 |
| `Missing LLM_API_KEY` | 检查 `.env` 是否位于 `backend`，Key 是否留空，以及启动命令是否使用 `dotenv -f .env run --`。 |
| 百炼返回 401 或 403 | 检查 API Key 是否有效、是否属于对应百炼账号。 |
| `Model request timed out` | 检查网络、模型名称和当前代码版本；百炼适配器需要包含关闭推理的兼容处理。 |
| 端口 8000 已被占用 | 停止旧服务，或把启动命令和访问地址中的 `8000` 改为其他端口。 |
| 页面可以打开但 OCR 失败 | 先确认 `/health/ready`，再检查模型目录和图片是否为 PNG/JPEG。 |

## 11. 局域网访问与安全边界

默认 `--host 127.0.0.1` 只允许本机访问。需要局域网测试时可改为：

```bash
dotenv -f .env run -- python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

当前是单进程、单用户演示，没有登录认证，不要直接暴露到公网。`.env`、`.venv`、`models` 和 `data` 都是本机运行资料，不应提交到 Git。
