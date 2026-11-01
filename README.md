# mwp-downloader

基于 NiceGUI 的下载器。启动后会自动监视项目根目录下的 `data` 文件夹，并处理新放入的 `*.json` 清单。下载任务并行执行，同一主机名最多使用 8 个连接。

## 启动

使用 Python 3.14 或更高版本安装项目依赖，然后运行：

```powershell
uv sync
uv run python main.py
```

## 构建 Windows 单文件发行版

在 64 位 Windows 上运行以下命令，使用 Nuitka 和 Zig 编译独立的单文件程序：

```powershell
.\build.ps1
```

首次构建会通过 Nuitka 下载 Zig 编译器。发行文件生成于 `dist\mwp-downloader.exe`；将该 EXE 单独分发即可。程序运行时会在 EXE 同级目录创建 `data` 文件夹，把 JSON 清单放入该目录。相对下载路径也以 EXE 所在目录为基准。使用 native 窗口时，目标 Windows 设备需要可用的 Microsoft Edge WebView2 Runtime。

打开终端显示的本地地址即可查看任务状态。页面支持直接填写下载链接和保存路径添加任务，手动添加无需提供 SHA1；如果没有文件大小信息，页面会显示已接收的数据量。清单中的 SHA1 也是可选项：填写时会校验，不填写时直接下载。保存路径的父目录不存在时会自动创建。SHA1 校验失败会从头重试 3 次（加上首次下载共 4 次），仍不匹配则静默跳过并在任务列表中标记“已跳过”。相对保存路径以项目根目录为基准，绝对路径也可以使用。未完成的 `.part` 文件会通过 HTTP Range 请求续传；若服务器忽略 Range 请求则会从头开始，避免拼接出损坏文件。

`data` 目录中的清单会在其所有下载项完成或跳过后自动删除；仍有下载失败的清单会保留。

## 下载清单

每个清单可以使用 URL 映射对象。`sha1` 字段可省略：

```json
{
  "https://example.com/archive.zip": {
    "path": "downloads/archive.zip",
    "sha1": "0123456789abcdef0123456789abcdef01234567"
  },
  "https://mirror.example.com/map.dat": {
    "path": "downloads/map.dat",
    "sha1": "89abcdef0123456789abcdef0123456789abcdef"
  }
}
```

也支持下载项数组：

```json
[
  {
    "url": "https://example.com/archive.zip",
    "path": "downloads/archive.zip",
    "sha1": "0123456789abcdef0123456789abcdef01234567"
  }
]
```

将 UTF-8 JSON 文件放入 `data` 文件夹。每个清单下载项都需要 HTTP(S) URL 和保存路径；可选的 `sha1` 必须是 40 位十六进制字符串。程序发现清单后会立即开始下载；运行期间新增的清单也会自动处理。
