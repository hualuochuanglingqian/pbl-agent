"""演示模式数据：无 API Key / 演示讲解时一键载入的完整案例。"""
from __future__ import annotations

from .schema import (Case, LearningBlock, MindmapNode, Question, Reference,
                     REF_SOURCE_MANUAL)


def demo_case() -> Case:
    case = Case(
        title="社区获得性肺炎的诊治思维（演示案例）",
        course="临床医学 · 呼吸系统疾病",
        audience="本科三年级",
        case_text=(
            "患者，男，68 岁，退休教师。\n"
            "主诉：发热、咳嗽 4 天，加重伴气促 1 天。\n"
            "现病史：4 天前受凉后出现发热，体温最高 38.9℃，伴寒战；咳嗽，咳黄脓痰，量中等，无异味；"
            "1 天前症状加重，出现活动后气促，上二层楼即感胸闷。\n"
            "既往史：2 型糖尿病 8 年，口服二甲双胍控制，空腹血糖 8~9 mmol/L；高血压 5 年。吸烟 30 年，约 20 支/日，未戒。\n"
            "查体：T 38.6℃，P 102 次/分，R 24 次/分，BP 135/85 mmHg；急性病容，口唇无明显发绀；"
            "右下肺叩诊浊音，呼吸音减低，可闻及湿啰音；心率 102 次/分，律齐。\n"
            "辅助检查：血 WBC 13.6×10⁹/L，NEU% 86%；CRP 96 mg/L；SpO₂ 93%（未吸氧）；"
            "胸部 CT 示右下叶大片实变影，伴支气管充气征；血糖 9.8 mmol/L。\n"
            "初步诊断：社区获得性肺炎（右下肺）；2 型糖尿病；高血压。"
        ),
        keywords=["社区获得性肺炎", "诊断与鉴别", "抗菌治疗", "血糖管理", "戒烟干预"],
        disease_mindmap=MindmapNode(
            text="社区获得性肺炎", children=[
                MindmapNode(text="病因与危险因素", children=[
                    MindmapNode(text="肺炎链球菌最常见"),
                    MindmapNode(text="非典型病原体：支原体/衣原体"),
                    MindmapNode(text="高龄、糖尿病、吸烟")]),
                MindmapNode(text="发病机制", children=[
                    MindmapNode(text="病原体入侵→肺泡渗出实变"),
                    MindmapNode(text="高血糖致中性粒细胞功能受损")]),
                MindmapNode(text="临床表现", children=[
                    MindmapNode(text="发热、咳脓痰、气促"),
                    MindmapNode(text="右下肺实变体征")]),
                MindmapNode(text="辅助检查", children=[
                    MindmapNode(text="WBC↑ NEU%↑ CRP↑"),
                    MindmapNode(text="胸部CT：大片实变+支气管充气征")]),
                MindmapNode(text="诊断与鉴别", children=[
                    MindmapNode(text="CAP诊断标准+CURB-65评分"),
                    MindmapNode(text="鉴别：肺结核/肺癌/肺栓塞")]),
                MindmapNode(text="治疗", children=[
                    MindmapNode(text="β-内酰胺类±大环内酯，或呼吸喹诺酮"),
                    MindmapNode(text="血糖管理：胰岛素，目标7~10 mmol/L"),
                    MindmapNode(text="48~72h评估疗效")]),
                MindmapNode(text="预防与管理", children=[
                    MindmapNode(text="5A戒烟干预"),
                    MindmapNode(text="1个月后复查CT与血糖")]),
            ]),
        questions=[
            Question(
                qid="Q1", text="该患者社区获得性肺炎的诊断依据有哪些？需要与哪些疾病鉴别？",
                qtype="诊断", en_keywords="community acquired pneumonia, diagnosis, differential",
                cn_keywords="社区获得性肺炎 诊断 鉴别",
                learning=[
                    LearningBlock(
                        heading="CAP 的诊断标准",
                        body="社区获得性肺炎（CAP）诊断依据：①新近出现的咳嗽、咳痰或原有呼吸道症状加重，"
                             "并出现脓性痰；②发热；③肺实变体征和（或）湿啰音；④WBC>10×10⁹/L 或<4×10⁹/L；"
                             "⑤胸部影像学显示片状、斑片状浸润影。符合 1~4 项中任意一项加第 5 项，"
                             "并除外肺结核、肺部肿瘤等疾病即可建立临床诊断。",
                        source="《内科学》（第10版）呼吸系统疾病篇"),
                    LearningBlock(
                        heading="主要鉴别诊断",
                        body="需与以下疾病鉴别，鉴别要点见表：\n"
                             "其中肺癌为本例重点排除对象：患者吸烟 30 年为高危因素，"
                             "抗感染治疗后需复查胸部 CT 以排除阻塞性肺炎。",
                        source="《内科学》（第10版）呼吸系统疾病篇",
                        table=[
                            ["鉴别疾病", "关键鉴别点", "针对本例"],
                            ["肺结核", "低热盗汗、痰抗酸染色/T-SPOT 阳性", "本例急性高热起病，暂不支持"],
                            ["肺癌/阻塞性肺炎", "刺激性咳嗽、消瘦、同一部位反复炎症", "吸烟 30 年，需抗治疗后复查 CT"],
                            ["肺血栓栓塞", "呼吸困难、咯血、D-二聚体升高、CTPA 确诊", "必要时查 D-二聚体"],
                        ]),
                    LearningBlock(
                        heading="病情评估",
                        body="（1）重症肺炎评估：采用 CURB-65 评分（意识、尿素氮、呼吸频率、血压、年龄），\n"
                             "（2）本例 SpO₂ 93% 提示低氧血症，需警惕重症倾向，建议住院治疗。",
                        source="《内科学》（第10版）呼吸系统疾病篇"),
                ],
                references=[
                    Reference(title="（演示）Diagnostics and management of community-acquired pneumonia",
                              authors=["Wunderink RG", "Waterer GW"], journal="Lancet",
                              year="2017", pmid="演示占位",
                              relevance="演示占位：配置 Key 后为真实 PubMed 文献", source="PubMed"),
                ],
                clinical=(
                    "本例为老年男性，急性起病，发热、咳黄脓痰，右下肺实变体征（叩诊浊音、呼吸音减低、湿啰音），"
                    "WBC 13.6×10⁹/L、NEU% 86%、CRP 96 mg/L 明显升高，CT 示右下叶大片实变伴支气管充气征，"
                    "符合 CAP 典型表现。鉴别上：患者无低热盗汗，暂不支持肺结核；吸烟 30 年为肺癌高危因素，"
                    "抗治疗后需复查 CT 排除阻塞性肺炎；SpO₂ 93% 提示低氧血症，需警惕重症倾向。"
                    "同时糖尿病患者感染易加重，控制血糖是治疗基础。"),
            ),
            Question(
                qid="Q2", text="该患者应如何选择初始抗感染方案？糖尿病对治疗有何影响？",
                qtype="用药", en_keywords="community acquired pneumonia, antibiotic therapy, diabetes",
                cn_keywords="社区获得性肺炎 抗菌药物 治疗",
                learning=[
                    LearningBlock(
                        heading="经验性抗感染治疗原则",
                        body="CAP 初始治疗按病情分层：门诊轻症可选青霉素类/第一代头孢/呼吸喹诺酮；"
                             "住院非重症单用β-内酰胺类或呼吸喹诺酮；有基础病（如糖尿病）者需覆盖"
                             "肺炎链球菌及流感嗜血杆菌，必要时联合。给药 48~72 小时评价疗效。",
                        source="《内科学》（第10版）呼吸系统疾病篇"),
                    LearningBlock(
                        heading="糖尿病合并感染的注意事项",
                        body="高血糖利于细菌繁殖并削弱中性粒细胞功能，感染应激又使血糖更难控制，形成恶性循环。"
                             "治疗中需：积极控糖（必要时胰岛素）、加强营养支持、警惕血糖波动与二重感染。",
                        source="《内科学》（第10版）内分泌与代谢性疾病篇"),
                ],
                references=[],
                clinical=(
                    "本例 68 岁、有糖尿病与高血压基础、SpO₂ 93%，属于需住院治疗的非重症 CAP。"
                    "经验性方案可选β-内酰胺类（如头孢曲松）± 大环内酯类，或单用呼吸喹诺酮；"
                    "同时暂停二甲双胍改用胰岛素控制血糖（应激状态、警惕乳酸酸中毒），"
                    "监测空腹血糖维持在 7~10 mmol/L；戒烟宣教应贯穿全程。48~72 小时后"
                    "根据体温、痰性状、血象变化评估疗效，无效需考虑耐药菌、阻塞因素或非典型病原体。"),
            ),
            Question(
                qid="Q3", text="作为接诊医生，应如何对该患者进行戒烟与长期健康管理？",
                qtype="人文", en_keywords="smoking cessation, primary care, elderly",
                cn_keywords="戒烟 健康管理 慢病管理",
                learning=[
                    LearningBlock(
                        heading="戒烟干预的 5A 法",
                        body="5A 法：询问（Ask）吸烟情况、建议（Advise）明确戒烟、评估（Assess）戒烟意愿、"
                             "帮助（Assist）制定计划与药物辅助（尼古丁替代/伐尼克兰）、"
                             "安排（Arrange）随访。每次就诊都是干预时机。",
                        source="《内科学》（第10版）呼吸系统疾病篇"),
                ],
                references=[],
                clinical=(
                    "本例吸烟 30 年、每日 20 支，既是 CAP 危险因素也是肺癌、COPD 高危因素。"
                    "借助本次住院的「 teachable moment 」效应，由团队共同完成 5A 干预："
                    "出院前制定戒烟计划并转介戒烟门诊；同时对糖尿病、高血压建立随访档案，"
                    "1 个月后复查胸 CT 与血糖，形成「治疗-教育-随访」闭环。"),
            ),
        ],
    )
    for q in case.questions:
        for r in q.references:
            r.source = REF_SOURCE_MANUAL if not r.relevance else r.source
    case.touch()
    return case
