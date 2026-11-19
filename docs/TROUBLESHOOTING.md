# 新环境排错

| 现象 | 检查与处理 |
| --- | --- |
| 找不到 Docker、无法连接 daemon | 确认 `docker version` 能同时显示 Client/Server。安装和启动 Docker 后再预检；无权限时先 `sudo -v`，运行入口加 `--sudo`。也支持 `--docker /path/to/run-docker`。 |
| apt 提示锁被占用 | 查看系统更新是否仍在工作，等它完成；不要删除锁文件或强制终止 dpkg。预检不负责安装 Docker。 |
| GitHub/Docker Hub 连接超时 | 在能联网的电脑下载 Release 的完整交付文件，传到目标机器，先核对 `DELIVERY_SHA256SUMS` 再 `docker load`。实验容器使用 `--network none`。 |
| 只有 Git clone，没有镜像 | 大型交付归档放在 Release。仓库的 `.gitignore` 排除了 `delivery/`，Git clone 不会自动取得归档。 |
| 镜像 ID 与旧记录不同 | Docker 的不同存储方式可能显示 OCI manifest 摘要或 image config 摘要。核对归档 SHA-256，再核对 `image-manifest.json` 的 config/RootFS 信息及镜像平台。预检还核对镜像与工作副本的源码清单、全部工具清单和实际执行，不把两个不同类型的摘要直接比较。 |
| 提示本地脚本与镜像不同 | 使用同一个 repro-v2 Release 的材料包和镜像。v1 镜像不含负对照修正，不能只更新 README 后继续用它。 |
| 硬件 perf 显示 not supported | KVM/WSL 可能没有开放 PMU；管理员权限也未必有用。本实验仍可插桩训练和计时，记录限制即可。不要伪造 cycles、IPC 或 LBR。 |
| CPU 0 不可用或 `CPU_ID` 为空 | 使用统一入口；脚本从容器允许列表选两个 CPU。检查 `host-preflight-*.json` 的 `container_probe`，不要复制别的机器 CPU 编号。 |
| 结果目录无写权限 | 结果根目录应属于当前普通用户。统一入口以当前 UID/GID 运行容器并测试写入；不要把已有结果改成所有人可写。 |
| 提示系统更新/编译正在运行 | 等任务完成后再启动正式计时；不要自动杀掉共享服务器上的任务。常驻的 unattended-upgrade-shutdown 等待进程不当作活跃更新。 |
| 负对照 ELF 校验失败 | 查看 `runtime-equivalence.json` 或 `console.log`。除下述已确认的元数据差异外，任何代码、数据或程序头变化仍需停止分析，不能跳过校验。 |

## 负对照误报的修正范围

2026-11-19 原镜像的 `negative` 步骤在正式计时前报 `OFF and ON load segments differ on nonmatching input`。BOLT 在非分配节 `.note.bolt_info` 保存自己的命令行；OFF/ON 字符串和输出路径长度不同，使文件末尾节表偏移跨过对齐边界。可加载范围内只有 ELF64 头的 `e_shoff` 字段发生变化，执行代码和数据相同。

v2 的校验会同时检查：

1. 必须是支持的 x86-64 ELF64；入口、ELF 头的其他字段、全部程序头相同。
2. `.note.bolt_info` 必须是非分配 SHT_NOTE，且包含已知 BOLT 说明格式；该节和节表都不能与任何程序段重叠。
3. 所有其他节的头、内容及顺序相同；只有该 note 的内容/大小允许变化。
4. 仅在该 note 确实变化时允许节表偏移变化；比较可加载内容时只归一化文件头 `[40,48)` 的 `e_shoff`，其余字节仍须相同。

这项校验不修改任何程序文件，也不声称应用输出测试可以代替代码核对。回归测试包含元数据差异通过、指令/数据/入口/权限变化失败，以及截断 ELF、落在程序段内的节表、带分配标志的 note 等拒绝样例。实现不依赖可被 `python -O` 关闭的 `assert`。

完整原始失败和辅助恢复发生在 v1；v2 已把处理纳入正式脚本，不再需要为某台服务器单独挂载恢复脚本。
