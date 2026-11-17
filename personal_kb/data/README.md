# 运行时数据（不入库）

本目录存放个人知识库的运行时数据，**全部不入 Git**（含平台题库内容与个人学习记录）：

| 文件 | 内容 |
|---|---|
| `platform_bank.json` | 本地题库（平台自测题 + 章节分类题，含答案解析） |
| `platform_folders.json` | 平台题库目录树（578 文件夹 / 41,729 题） |
| `platform_wrongbook.json` | 平台错题集导出 |
| `papers/*.json` | 各套自测试卷的抓取结果 |
| `folder_sweep/*.json` | 按章节文件夹扫描的题目 |
| `selftests.json` | 本地自测记录（含答题草稿） |
| `self_wrong.json` | 自测错题本 |
| `question_marks.json` | 题目标记 |
| `ai_solutions.json` | AI 解答缓存 |
| `backups/` | 上述文件的自动备份（保留最近 40 份） |

首次运行后由程序自动生成。题库内容来自学校教学平台，仅供个人学习，请勿公开分发。
