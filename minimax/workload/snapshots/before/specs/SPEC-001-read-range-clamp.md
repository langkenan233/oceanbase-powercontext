# SPEC-001 代码读取范围超出文件行数时必须报告截断

revision: 1

## 适用范围

`linewindow.reader.read_range`，即业务工程 `linewindow` 的行区间读取函数。

## 要改什么

当一次读取请求的 `end` 大于目标内容实际拥有的行数时，函数必须把读取范围截断到
内容末尾，**并且**在返回结果的 `limitations` 中加入 `source_range_clamped` 标记，
同时把 `truncated` 置为 `True`。

截断后的 `end_line` 必须等于内容的实际行数。

## 哪些原有行为必须保持

以下行为必须保持不变：

1. `start` 超过内容实际行数时，仍然抛出 `RangeError`，且 `code` 为
   `code_range_missing`。
2. `start` 不超过内容实际行数时，读取到的内容与修改前完全一致。
3. 请求范围完全落在内容之内时，不加入 `source_range_clamped`。
4. 请求的 `end` 恰好等于实际行数时，不加入 `source_range_clamped`。
5. 因为单行内容过长而截断内容时，仍然使用既有的 `source_truncated` 标记。
6. 因为单行内容过长而无法返回任何内容时，仍然使用既有的
   `source_line_too_large` 标记。
7. `source_range_clamped` 与 `source_truncated` 可以同时出现。

## 理由

修改前，请求第 100 到 300 行而内容只有 150 行时，函数静默地把 `end_line` 改成
150，既不报错也不留下任何限制标记。调用方无法区分「内容本身只有 150 行」和
「我请求的范围被截断了」，因此会在基于不完整证据的前提下继续工作。
