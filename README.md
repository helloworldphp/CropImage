# Image Crop MCP（魔搭 ModelScope 托管版）

图片 URL + 坐标裁剪 → 上传阿里云 OSS → 返回 URL。  
默认 **STDIO**，供 [ModelScope MCP 广场](https://modelscope.cn/mcp) / 函数计算托管（平台自动转 SSE）。

> 内网生产请继续用原 `cropImage` 服务；本目录是独立的公网托管副本。

## 目录

```
cropImageMcp/
├── server.py          # MCP 入口（默认 stdio）
├── crop_service.py    # 裁剪 + OSS
├── mcp_config.json    # 魔搭部署配置
├── requirements.txt
├── .env.example
└── README.md
```

## 环境变量

在魔搭创建服务时配置（勿提交 `.env`）：

| 变量 | 说明 |
|------|------|
| `OSS_ACCESS_KEY_ID` | 阿里云 AK |
| `OSS_ACCESS_KEY_SECRET` | 阿里云 SK |
| `OSS_BUCKET_NAME` | Bucket |
| `OSS_ENDPOINT` | 如 `oss-cn-hangzhou.aliyuncs.com` |
| `OSS_UPLOAD_PATH_PREFIX` | 对象前缀，默认 `crop` |

**注意：** `image_url` 必须是公网可访问地址；内网图无法被魔搭侧实例下载。

## 发布到 ModelScope

1. 将本目录推到 GitHub（不要包含 `.env`）
2. 打开 [MCP 广场](https://modelscope.cn/mcp) → **创建 MCP 服务**
3. 填写：
   - **类型**：STDIO
   - **GitHub 仓库**：你的仓库地址
   - **配置文件路径**：`mcp_config.json`
   - **部署方式**：个人阿里云函数计算资源
4. 配置上表 OSS 环境变量后提交
5. 部署成功后使用平台下发的 SSE URL（带鉴权）在 MCP 实验场测试

`mcp_config.json` 启动命令为 `python server.py`（默认 `MCP_TRANSPORT=stdio`）。

## 本地调试

```bash
cd cropImageMcp
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 填 OSS

# STDIO（与魔搭一致）
python server.py

# 或本地起 SSE
MCP_TRANSPORT=sse python server.py
```

Inspector：

```bash
npx @modelcontextprotocol/inspector
# Transport: STDIO，Command: python，Args: server.py
```

## MCP 工具

| 工具 | 说明 |
|------|------|
| `crop_image_by_bbox` | `image_url` + `x,y,width,height` |
| `crop_image_by_xyxy` | `image_url` + `x1,y1,x2,y2` |
| `crop_image_batch` | 同一张图多个 bbox |
| `get_image_size` | 返回宽高 |
| `upload_file` | base64 上传 OSS |
| `resize_image` | 缩放到指定宽高 |
| `compose_image_back` | 将 cropped 贴回 remaining |
| `add_border` | 加边框 |
