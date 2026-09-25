# vendor 说明

面向浏览器直接加载的第三方依赖副本。**故意不用 CDN** —— 演示现场在教室，网络不可靠，这是本项目唯一真正对外部网络有依赖的地方，用本地文件把它彻底摘掉。

下载时间：2026-09-24
下载方式：`D:/conda_envs/so101-twin/Scripts/python.exe twin/tools/fetch_assets.py`（可重跑，已存在的会跳过）

## 版本

| 包 | 版本 | 主源 |
|---|---|---|
| three | 0.186.0 | `registry.npmmirror.com/three/0.186.0/files/...`（备源 jsdelivr / unpkg） |
| urdf-loader | 0.13.1 | `registry.npmmirror.com/urdf-loader/0.13.1/files/...`（备源 jsdelivr / unpkg） |

## 文件与作用

```
three/three.module.js                              three 主入口（bundle）
three/three.core.js                                three.module.js 直接依赖它，必须同目录
three/examples/jsm/loaders/STLLoader.js            读 SO-101 的 13 个 STL
three/examples/jsm/loaders/ColladaLoader.js        ┐
three/examples/jsm/loaders/TGALoader.js            │ urdf-loader 静态 import 了 ColladaLoader，
three/examples/jsm/loaders/collada/ColladaParser.js│ 它又拖着这三个。SO-101 走不到这条分支，
three/examples/jsm/loaders/collada/ColladaComposer.js 但因为 import 是静态的，浏览器在模块加载
three/examples/jsm/controls/OrbitControls.js       阶段就会去取 —— 缺一个就整页白屏
urdf-loader/src/URDFLoader.js                      URDF 解析
urdf-loader/src/URDFClasses.js                     URDFRobot / URDFJoint / URDFLink 等
```

`examples/jsm/` 下的目录层次不能压平：`ColladaLoader.js` 用的是相对路径 import（`../loaders/TGALoader.js`、`./collada/ColladaParser.js`），换个位置就找不到。

## 升级

改 `tools/fetch_assets.py` 顶部的 `THREE_VER` / `URDF_LOADER_VER`，然后：

```
D:/conda_envs/so101-twin/Scripts/python.exe twin/tools/fetch_assets.py --force
```

脚本下完会自己扫一遍 import 闭包，缺文件直接报出来。这一步别省 —— 闭包不全的症状是「页面全白 + 报错指向入口文件」，从症状反推很费时间。

`urdf-loader` 的 peerDependency 只要求 `three >= 0.152`，但它自己的 devDependency 停在 three 0.164，与 0.186 没有官方测过。真撞上 API 不兼容，把 `THREE_VER` 降到 0.164 重跑即可，目录结构不用动。
