# 随作品交付的 PowerContext 运行期

`powercontext/` 是 OceanBase PowerContext 的源码副本，放在这里是为了让本作品在
评审的干净环境里可以直接启动 Server，不依赖仓库其他分支或本地文件。

| 项 | 值 |
| --- | --- |
| 上游项目 | [oceanbase/powercontext](https://github.com/oceanbase/powercontext) |
| 来源分支 | `2026-competition` |
| 来源 commit | `b3106895761a964056edded6964f4ecf9032b133` |
| API 版本 | `1.2.0`（`GET /openapi.json` 的 `info.version`） |
| 许可证 | Apache License 2.0 |
| 修改内容 | 无。`powercontext/` 与上游 `src/powercontext/` 逐文件一致（`diff -r` 无差异） |

## 为什么要随作品交付

赛题指定的开发分支 `2026-competition-ecosystem` 只有各参赛方的目录，没有
PowerContext 源码（`src/` 只在 `2026-competition` 分支上）。题面要求「能够运行」
这一项「不依赖未交付的本地文件或状态」，所以这里把运行 Server 需要的源码一并提交。

## 怎么用

`minimax/scripts/start_server.py` 会自己找到这份副本：先找仓库根目录的 `src/`，
找不到就用 `minimax/vendor/`。两份都存在时用 `minimax/vendor/`，因为它是评审实际
拿到的提交内容。

```bash
python minimax/scripts/start_server.py run --env-file minimax/powercontext.env
```

第三方依赖不在这里，需要单独安装，见 `minimax/README.md` 第二节。

## 备选安装方式

PowerContext 在 PyPI 上公开发布，也可以不提交这份副本，直接安装：

```bash
pip install "powercontext[server,cli]==1.5.0"
```

提交这份副本是为了在评审环境里锁定到与开发时完全一致的版本。
