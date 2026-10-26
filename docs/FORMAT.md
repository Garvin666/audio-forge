# NCM 容器格式说明

本文是 `src/audioforge/ncm.py` 的格式依据。所有结论都来自对**实测样本**的逐字节核对
（两个 320 kbps / 48 kHz 立体声样本，约 9–10 MB），而不是从别处抄来的伪代码。

> 阅读前提醒：`.ncm` 是网易云客户端的**私有容器**，不是公开标准。字段含义会随客户端
> 版本漂移，本文记录的是「实测能解出来」的那一版，遇到新格式请以字节为准重新核对。

## 1. 整体布局

全部小端（little-endian）：

| 偏移 | 长度 | 字段 | 说明 |
| --- | --- | --- | --- |
| 0 | 8 | magic | 固定 `CTENFDAM`（ASCII） |
| 8 | 2 | gap | 实测恒为 `01 70`，读时直接跳过，不校验 |
| 10 | 4 | `key_len` | key 块字节数（u32） |
| 14 | `key_len` | `key_data` | 每字节 `^ 0x64` 后做 AES-128-ECB 解密 |
| … | 4 | `meta_len` | meta 块字节数（u32），可为 0 |
| … | `meta_len` | `meta_data` | 每字节 `^ 0x63`，去前缀、base64、AES 解密，得 JSON |
| … | 4 | `crc32` | 本工具不校验 |
| … | 1 | gap | 固定 0x00 |
| … | 4 | `cover_len` | 封面字节数；**新版客户端会连写两遍**，见 §4 |
| … | `cover_len` | `cover` | 原始 JPEG 或 PNG 字节 |
| … | 剩余 | `audio` | 加密码流，256 字节周期 keystream XOR |

`audio` 段没有二次编码 —— 异或还原后就是原始 mp3 / flac 码流。所以「解密」即可播放，
换格式必须再走一次 ffmpeg。

## 2. key 块

两个固定密钥：

```
CORE_KEY = 687A4852416D736F356B496E62617857   # "hzHRAmso5kInbaxW"
META_KEY = 2331346C6A6B5F215C5D2630553C2728   # "#14ljk_!\]&0U<'("
```

> 注意第二条的尾巴是 `<` `'` `(`（`3C 27 28`），**不是** `<` `(` `'`。
> 不少现成脚本的注释把这两个字符写反了，照抄注释会得到一堆乱码 JSON。
> 排查时一定以 hex 为准 —— 这个坑在本项目里真实踩过一次。

key 块解密后有**两种布局**，用 `key_len % 16` 区分：

- **余 0（新版）**：整块都是密文。解密后明文以 `neteasecloudmusic` 开头，其后是变长密钥
  （实测两个样本分别是 **98 与 99 字符**，长度本身可变，绝不能按 32 字节硬截）。末尾是 PKCS#7 补位。
- **余 1（旧版）**：密文前面挂了 17 字节**明文**前缀 `neteasecloudmusic`，要先切掉再解密。

解出的密钥是 ASCII 字符串，用来生成 keystream。

## 3. meta 块

```
明文 = b"163 key(Don't modify):" + base64( AES-ECB(META_KEY, b"music:" + JSON) )
```

拆解顺序（少一步就出不来）：

1. 整块每字节 `^ 0x63`
2. 去掉 22 字节前缀 `163 key(Don't modify):`
3. base64 解码（长度应为 16 的整数倍）
4. AES-128-ECB 解密，去 PKCS#7 补位
5. 去掉 `music:` 前缀，剩下的才是 JSON

JSON 里可用的字段：

| 字段 | 含义 | 备注 |
| --- | --- | --- |
| `musicName` | 曲名 | 可能缺失 |
| `artist` | `[["名字", "内部ID"], …]` | **下标 0 是名字**，1 是 ID |
| `album` | 专辑名 | |
| `bitrate` | 码率（bps） | 实测 320000 |
| `duration` | 时长（毫秒） | 实测 245950 |
| `format` | `mp3` / `flac` | 仅作参考，**必须用码流头校正** |

`format` 不能全信：解析失败或字段缺失时会退化，所以本工具解密前会先解出音频段前 4 KB，
按真实魔术字节（`fLaC` / `ID3` / `0xFFFB` …）决定输出扩展名。

## 4. 封面区

封面长度字段存在两种写法，靠「下一段是不是图片魔术字节」区分，**不能靠版本号猜**：

```
写法 A（单字段）:  u32 cover_len | cover_bytes
写法 B（重复长度）: u32 cover_len | u32 cover_len | cover_bytes
```

判定逻辑：

1. `cover_len == 0` → 无封面，音频紧跟其后
2. 若紧随的 4 字节是 `FF D8 FF`（JPEG）或 `89 50 4E 47`（PNG）→ 写法 A
3. 若紧随的 4 字节解出的整数等于 `cover_len` → 写法 B
4. 其余情况按写法 A 处理，并校验 `cover_len` 不超过剩余字节数，否则判定为无封面

第 4 条是必须的兜底：真实文件里出现过首次解析把音频头当成封面长度的情况，
不校验会吞掉整段音频。

## 5. 音频 keystream

这是 NCM 唯一"自研"的密码学部分：它不是标准 RC4，而是 **RC4 的 KSA + 展开后的 PRGA**。

设密钥字符串为 `k`（长度 `n`）：

```python
box = list(range(256))
j = 0
for i in range(256):
    j = (j + box[i] + ord(k[i % n])) & 0xFF
    box[i], box[j] = box[j], box[i]  # 标准 RC4 KSA
```

关键点：PRGA 的第 `i` 个字节只取决于 `i % 256`，因为位置索引本来就 `& 0xFF`。
所以可以把整条流"预展开"成一张 256 字节表：

```python
table = bytearray(256)
for p in range(256):
    q = (p + 1) & 0xFF
    table[p] = box[(box[q] + box[(box[q] + q) & 0xFF]) & 0xFF]
```

解密就是 `audio[i] ^= table[i % 256]`。加解密同一个操作（异或自反）。

### 为什么值得这么写

朴素写法是逐字节 Python 循环。实测 9.4 MB 音频：

| 实现 | 耗时 | 吞吐 |
| --- | --- | --- |
| 逐字节 `for i in range(len(data))` | 1.75 s | 5.4 MB/s |
| 本实现：周期表 + 4 MiB 分块 `int` 异或 | 0.033 s | **287 MB/s** |

**53 倍**。做法是把每块的 4 MiB 字节串当成一个大整数做一次异或：

```python
int.from_bytes(buf, "little") ^ int.from_bytes(stream, "little")
```

前提是**每块起点必须落在 256 的周期边界上**，否则相位错位、整段音频变成噪音。
因此分块大小取 256 的整数倍（4 MiB = 16384 × 256），并且一旦出现短读就直接报错，
绝不让后续分块带着错的相位继续跑。

## 6. 三十行版本

把上面串起来，够自己验证一个文件：

```python
import base64, json, struct
from audioforge.aes import AES128ECB, pkcs7_unpad
from audioforge.ncm import CORE_KEY, META_KEY, keystream_table

raw = open("song.ncm", "rb").read()
assert raw[:8] == b"CTENFDAM"
pos = 10

n = struct.unpack("<I", raw[pos : pos + 4])[0]
pos += 4
key = AES128ECB(CORE_KEY).decrypt(bytes(b ^ 0x64 for b in raw[pos : pos + n]))
key = pkcs7_unpad(key)[17:].rstrip(b"\x00").decode()  # 跳过 neteasecloudmusic 前缀
pos += n

n = struct.unpack("<I", raw[pos : pos + 4])[0]
pos += 4
meta = b""
if n:
    body = bytes(b ^ 0x63 for b in raw[pos : pos + n])[22:]
    meta = json.loads(pkcs7_unpad(AES128ECB(META_KEY).decrypt(base64.b64decode(body)))[6:])
pos += n

pos += 4 + 1  # crc32 + gap
n = struct.unpack("<I", raw[pos : pos + 4])[0]
pos += 4
if n and struct.unpack("<I", raw[pos : pos + 4])[0] == n:  # 新版把长度写了两遍
    pos += 4
pos += n  # 跳过封面

table = keystream_table(key)
audio = bytes(raw[pos + i] ^ table[i % 256] for i in range(len(raw) - pos))
open("song.mp3", "wb").write(audio)
print(meta["musicName"], audio[:4].hex())
```

（这段是**慢速参考实现**，只用来对照理解，真实路径走 `ncm.decrypt()`。）

## 7. 已知坑清单

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| meta 解出乱码 | `META_KEY` 末尾 `<('` 抄错顺序 | 用 hex `…3C 27 28` |
| 密钥被截断成 32 字节 | 新版客户端密钥长度可变（实测 99） | 按解密结果全长取，不硬截 |
| 音频前几秒正常、后面全是噪音 | 分块起点脱离 256 周期 | 分块大小取 256 整数倍 + 短读即报错 |
| 输出文件为空或极短 | 封面长度字段被误读成音频长度 | 校验 `cover_len` 上界，异常则视为无封面 |
| 扩展名是 .mp3 但实际是 flac | meta 的 `format` 字段不可信 | 解出音频头部 4 KB 再判定 |
| Python 3.13 上 import cgi 失败 | 3.13 移除了 `cgi` 模块 | 本项目的 multipart 解析不依赖它 |
