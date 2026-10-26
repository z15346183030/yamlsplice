# yamlsplice

按路径读写 YAML。**只改你点的那几个字节**，注释、空行、缩进、引号、键顺序、emoji 键名、CRLF 全都原样留着。

```bash
python yamlsplice.py get spec.replicas deploy.yaml          # 3
python yamlsplice.py set spec.replicas 5 deploy.yaml -i     # 只改这一行
python yamlsplice.py del  spec.template.spec.containers[0].resources deploy.yaml -i
```

Python 3.8 以上，没有依赖，没有配置文件，一个文件。

---

## 它解决的是什么问题

绝大多数 YAML 工具干的是这件事：**读进来 → 变成内存里的对象 → 再吐出去**。

这个流程对程序友好，对人很不友好。因为「再吐出去」的那份文本，是工具按自己的审美重新排的：

- 你写 `- a`，它给你缩进成 `  - a`；
- 你两个键之间空一行分隔，它给你删掉；
- 你写的 `key:  # 说明` 双空格，它给你压成一个；
- 你的键名里有 emoji，它可能给你写坏；
- 它觉得 `bla: bla\nx: 1` 这种字符串该用 `|-`，就给你加个 `|-`。

改一个数字，git diff 出来四十行。评审的人得一行行看，才能确认你没顺手改别的东西。

`yamlsplice` 换了个做法：**不重建文件，只做手术。**

解析的时候不生成对象，只记下「这个值在原文的第几个字节到第几个字节」。写的时候把那一小段替换掉，其余字节一个都不碰。注释没被读进来，所以不会丢；缩进没被重新计算，所以不会变。

代价是它得自己判断每个值的边界——这件事比听起来麻烦，也是这个工具全部的复杂度所在。

---

## 装

不需要装。把 `yamlsplice.py` 拷到任何地方：

```bash
python yamlsplice.py --help
```

想当命令用，加个别名：

```bash
alias ysp='python /path/to/yamlsplice.py'
```

---

## 三十秒

用仓库里的 `examples/deployment.yaml`（一段带注释和空行的 k8s 配置）：

```bash
$ python yamlsplice.py get spec.template.spec.containers[0].image examples/deployment.yaml
nginx:1.25
```

改个镜像版本，看 diff：

```bash
$ python yamlsplice.py set spec.template.spec.containers[0].image nginx:1.27 examples/deployment.yaml > /tmp/new.yaml
$ diff examples/deployment.yaml /tmp/new.yaml
22c22
<           image: nginx:1.25
---
>           image: nginx:1.27
```

一行。上面那行 `# 演示用配置...`、中间那些空行、`containers:` 下面 `- name: web` 用两格缩进这种写法，全都没动。

读块标量，它会把 `|` 的折行规则算好再给你：

```bash
$ python yamlsplice.py get ops.runbook examples/deployment.yaml
1. 先看 kubectl get pods
2. 再看 kubectl logs
```

删一整块：

```bash
$ python yamlsplice.py del spec.template.spec.containers[0].resources examples/deployment.yaml
```

要机器读的格式：

```bash
$ python yamlsplice.py get spec --json examples/deployment.yaml
{
  "replicas": 3,
  "selector": {
    "matchLabels": {
      "app": "demo"
    }
  }
}
```

---

## 路径怎么写

| 写法 | 意思 |
|---|---|
| `spec.replicas` | 嵌套的键 |
| `containers[0].image` | 数组第 0 项 |
| `containers[-1]` | 数组最后一项 |
| `containers[+]` | 追加到末尾（只给 `set` 用） |
| `annotations["a.b"]` | 键里带点、空格、方括号时用方括号 |
| `.` | 整个文档 |

键里带点的必须用方括号。忘了也没关系，报错会告诉你：

```bash
$ python yamlsplice.py get spec.template.metadata.annotations.prometheus.io/scrape examples/deployment.yaml
yamlsplice: 这一层没有键 'prometheus'。有的键是：prometheus.io/scrape
提示：名字里带点或方括号的键要用方括号包起来，例如 ["prometheus.io/scrape"]

$ python yamlsplice.py get 'spec.template.metadata.annotations["prometheus.io/scrape"]' examples/deployment.yaml
true
```

---

## 写回去的值长什么样

新值会尽量沿用原来的写法，改完的文件看起来还是同一个人写的。

| 原来是 | 你写 | 结果 |
|---|---|---|
| `n: 1` | `set n 42` | `n: 42` |
| `n: "1"` | `set n 42` | `n: "42"`（保留引号） |
| `n: '1'` | `set n "it's"` | `n: 'it''s'` |
| `s: \|` 块 | 多行文本 | 还是 `\|` 块，缩进跟着父键走 |
| `a: x` | `set a true` | `a: true`（识别类型） |
| `a: x` | `set a 1.5 -r` | `a: "1.5"`（`-r` 当纯字符串） |
| `a: x` | `set a "[1, 2]"` | `a: [1, 2]`（flow 集合原样写入） |

需要引号的时候它会自己加：`#tag`、`a: b`、`-x`、`true`、`012345` 这些都会被加上双引号，免得读回来变成别的东西。

`--raw` / `-r` 的意思是「这就是个字符串，别猜类型」。不加的时候会认整数、小数、布尔、`null` 和 `[...]` / `{...}`。

---

## 常用命令

```bash
python yamlsplice.py get <path> <file> [--json]        # 读，打到屏幕
python yamlsplice.py set <path> <value> <file> [-i]    # 写，默认打到屏幕
python yamlsplice.py del <path> <file> [-i]            # 删，默认打到屏幕
```

- 不加 `-i` 就只输出，不动文件——可以先看一眼再决定。
- `<file>` 写 `-` 从标准输入读（只能配 `get`）。
- 退出码：`0` 成功、`1` 文件或语法问题、`2` 路径不存在、`3` 参数用错。

在脚本里判断路径存不存在：

```bash
if python yamlsplice.py get spec.replicas deploy.yaml >/dev/null 2>&1; then
  echo "有"
else
  echo "没有（退出码 $?）"
fi
```

---

## 它不动什么

这些不是「支持的特性」，是因为它们从来没被读进去再写出来，所以不可能被改：

- 注释（行首的、行尾的、块里面的）
- 空行，包括连续空行
- 缩进宽度（2 格、4 格、`- ` 跟父键齐平这种写法都保留）
- 键的顺序
- 引号风格（单引号、双引号、不加引号）
- 锚点 `&a` / 别名 `*a` 的原文
- Unicode 键名和值，包括 emoji
- CRLF 换行符、文件末尾有没有换行、BOM
- 多文档里的其他文档

---

## 它不做的事

诚实清单，省得你踩：

- **不解析别名。** `*base` 就是一段文本，`get` 会把它原样给你，不会去展开。
- **多文档只改第一份。** `---` 后面的文档原样保留，但路径只在前一份里找。
- **flow 集合不能逐键改。** `a: {x: 1}` 里想改 `x`，得整个 `a` 重写。往空 `{}` 里加键是可以的。
- **不支持复杂键**（`? key` 那种写法）和 `--- 内容` 写在同一行。
- **多行裸标量改写时会并成一行。** 语义不变，但那几行的折行位置找不回来了。
- **不做 schema 校验。** 它只管文本边界，不管你的 k8s 字段名对不对。

---

## 实测

拿 11 个真实文件（GitHub Actions 工作流、docker compose、istio bookinfo、helm 的 1270 行 values.yaml、traefik 的 5311 行 CRD），每个文件改第一个标量的值，数改了行数：

| 做法 | 平均改动行数 | 11 个文件里只改 1 行的 |
|---|---|---|
| **yamlsplice** | **1.0** | **11 / 11** |
| ruamel.yaml（round-trip 模式） | 118.7 | 1 / 11 |
| PyYAML（读完再 dump） | 333.5 | 1 / 11 |

注释：语料一共 658 行注释。yamlsplice 剩 658 行，ruamel 剩 658 行，PyYAML 剩 1 行。

速度（同一台机器，Windows，Python 3.13）：

| 文件 | yamlsplice 改一个值 | ruamel 读+写 | PyYAML 读+写 |
|---|---|---|---|
| traefik CRD，5,311 行 / 285 KB | 11.8 ms | 911 ms | 559 ms |
| 合成 compose，18,002 行 / 319 KB | 112.7 ms | 3,222 ms | 1,514 ms |

它快不是因为写得巧，是因为**它不需要构造那棵树**。读完就扔，只留一张「值在哪」的位置表。

另外做了 286 次「改一个值，然后用 PyYAML 把改前改后都读一遍」的检查，确认改动只落在目标路径上，别处没有任何变化。

---

## 测试

```bash
python -m unittest discover -s tests -v
```

78 个用例，覆盖：空行和注释的存活、CRLF、BOM、末尾无换行、emoji 键、块标量的读写、引号风格继承、各种路径写法、删除的边界、命令行退出码。

---

## 和 yq 的关系

[yq](https://github.com/mikefarah/yq) 是这条路的老大：16k 星，支持 YAML / JSON / XML / CSV / TOML / HCL / properties 七种格式，内置一门表达式语言，能写 `select(.age > 30) | .name`。

`yamlsplice` 只做一件事：**给我一个路径，我改那一个值，别动别的。**

没有表达式，没有 `-o=json`，没有七种格式。你想要的如果是「从一堆 YAML 里筛出符合条件的对象」，去用 yq；你想要的如果是「把这个字段从 3 改成 5，别给我搞出四十行 diff」，用这个。

细节见 [ANALYSIS.md](ANALYSIS.md)。

---

## 许可证

MIT。
