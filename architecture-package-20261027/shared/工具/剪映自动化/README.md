# 剪映自动化：草稿只读解析实验

当前结果见[测试结果](测试结果.md)。已在本机剪映11.6.0验证3个项目、15个文件。

所有产物位于本目录，原工程只读；输出为新生成的JSON和时间线CSV，没有原草稿备份。编解码运行于独立进程，禁止联网并限制写入范围。

## 重复使用

本机应用版本或库哈希变化时会停止。只使用read_draft.py入口运行，不直接调用原生二进制；这是本机实验入口，不是通用版本适配。

```sh
cd /Users/suzhizhou/Documents/私人助理/工具/剪映自动化
/Users/suzhizhou/Documents/私人助理/配置/venvs/core/bin/python read_draft.py '/原草稿目录'
```

每次创建独立结果目录，位置记入results/latest-run.txt。运行前确认有权读取指定项目。

## 来源

C++接口声明参考jy-draftc的MIT许可macOS示例；实现来自本机剪映安装，没有分发、复制或修改官方库。上游许可见LICENSE.jy-draftc。

- https://github.com/wenshui330/jy-draftc/tree/main/jy-draftc-mac
- https://github.com/wenshui330/jy-draftc/blob/main/jy-draftc-mac/jy-draftc-mac/EncryptUtil.h

读取通过不表示任意工程都可渲染，也不表示具备会员素材权益或已完成视觉验收。
