# 教材词表 JSON 格式（WORDBANK-FORMAT）

导入器：`python -m cairn.book_service <文件.json> [--subject 学科] [--db 数据库URL]`
函数入口：`cairn.book_service.import_book(session, book_dict)`

一份文件 = 一本书。三科（英语 / 语文 / 数学）共用同一套结构，学科只是 `subject` 的一个值。

## 结构

```json
{
  "subject": "英语",
  "publisher": "上海教育出版社",
  "grade": 5,
  "semester": "上",
  "name": "沪教版（沪教牛津版·三起）五年级上册",
  "source_note": "来源 https://…（抓取日 2026-10-21）",
  "units": [
    {
      "unit_no": 1,
      "title": "My future",
      "words": [
        { "position": 1, "cue": "将来；未来", "answer": "future", "extra": "ˈfjuːtʃə(r)" }
      ]
    }
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `subject` | 是 | 学科中文名：`英语` / `语文` / `数学`（与 `question.subject` 同口径）。文件里没有时用 `--subject` 补 |
| `publisher` | 是 | 出版社 |
| `grade` | 是 | 年级 1–12（整数）。页面显示成「五年级」 |
| `semester` | 是 | `上` / `下` |
| `name` | 是 | 书名（完整名字，含版本） |
| `source_note` | 否 | 来源说明（URL / 抓取日期 / 整理人）。**真实数据必须写** |
| `units[].unit_no` | 是 | 教材上的单元号，正整数；也接受 `unit` 作为别名 |
| `units[].title` | 否 | 单元标题（如 `My future`） |
| `units[].words[].cue` | 是 | 提示：孩子看到/听到的（中文意思、拼音、知识点提示）。别名 `cn` |
| `units[].words[].answer` | 是 | 要写的：英语单词、汉字、计算题答案。别名 `word` |
| `units[].words[].extra` | 否 | 读音/写法辅助：英语音标或语文拼音（同一列，不按学科分表）。别名 `ipa` |
| `units[].words[].position` | 否 | 单元内顺序，缺省按出现顺序 1、2、3… |

别名（`word`/`cn`/`ipa`、`unit`）是为了让三色卡已有的词表文件不改一个字节就能导。

## 幂等规则（重导不会翻倍）

- **认书**：`subject + publisher + grade + semester + name` 五件套是书的身份。
  同一本书必须沿用原始元数据；改了书名/出版社/年级/学期，会被当成另一本书（同一份词表会变成两本）。
- **认单元**：`(book, unit_no)`。
- **认条目**：`(unit, answer)`，比较时忽略大小写与空白（`Future` 与 `future` 算同一条）。
  同一个单元里同答案只留一条；不同单元出现同一个词是正常的，各自保留。
- 重导同一份文件：`新增 0 条、跳过 N 条`，三张表行数不变。
- 整份文件一个事务：任何一条数据不合规（缺 `answer`、单元号非法…）整批回滚，不会留下半本书。
- **缺 `cue` 会先走词典自动补**（词典卡）：语文条目用 CC-CEDICT 补拼音、英语条目用
  ECDICT 补中文释义（本地词典 `data/dict/dict.sqlite`，由 `scripts/build_dict.py`
  构建，缺词典不影响导入）。补不到的条目不落库，记为「待人工补全」
  （`ImportResult.needs_manual`，内存标记 + 日志），重导补齐后正常入库。

## 命令示例

```bash
# 导入自行准备且有权使用的词表（若文件里没有 subject，用 --subject 补）
CAIRN_DB_URL=sqlite:///./cairn.db \
  .venv/bin/python -m cairn.book_service \
  ./my-wordbank.json \
  --subject 英语

# 再次导入同一个文件不会重复添加条目
```

## 页面行为

- `GET /books`：左边选书（学科、年级、词数），右边按单元列条目；顶部搜索框跨单元搜
  （提示 / 答案 / 音标拼音三列都搜）。
- 勾选后点「加入错题库 N 条」→ 走 `entry_service.create_error_items`：
  `subject` = 书的学科，`lang` 按 `answer` 判定，语文词的拼音写进朗读 cue，全部从新题起步。
- 已经在错题库里的条目显示「已加入」，复选框不可勾（去重键与批量入库同一套，按答案、忽略大小写与空白）。

## 不编造内容

- 语文生字表、数学知识点等真实内容只能来自真实来源（教材目录、纸质书、老师给的词表）。
- 仓库里只放结构、导入器与测试用的少量样例；真实词表通过上面的命令导入本机数据库，不进仓库。
- 没有词表的书（目前人教版五上语文/数学是空书）页面显示「这本书还没有词表，等导入」。
