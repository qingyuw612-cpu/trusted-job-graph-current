"""内存 RoleStore 实现 — 内嵌少量示例数据，开箱即用（CI/测试/演示）。"""

from typing import Any, Dict, List, Optional

from .interface import RoleStore


def _skills(*items: tuple) -> List[Dict[str, Any]]:
    """构造技能列表：("技能名", "类别", 权重, 排名)。"""
    out = []
    for rank, (name, category, weight) in enumerate(items, start=1):
        out.append(
            {"name": name, "category": category, "weight": float(weight), "rank": rank}
        )
    return out


_SAMPLE_ROLES: List[Dict[str, Any]] = [
    {
        "role_name": "大模型算法工程师",
        "family_name": "算法",
        "domain_name": "AI",
        "jd_count": 3,
        "skills": _skills(
            ("大语言模型", "知识", 2.0),
            ("Transformer架构", "知识", 2.0),
            ("深度学习", "知识", 1.5),
            ("分布式训练", "知识", 1.0),
            ("PyTorch", "技术", 2.0),
            ("Python", "技术", 1.5),
            ("C++", "技术", 1.0),
            ("模型量化", "技术", 1.5),
            ("SFT微调", "技术", 2.0),
            ("RLHF", "技术", 1.5),
            ("LoRA", "技术", 1.0),
            ("数据清洗", "技术", 1.0),
            ("硕士及以上学历", "任职条件", 1.5),
            ("计算机相关专业", "任职条件", 1.0),
            ("对AI技术充满热情", "动机", 1.0),
            ("自驱力强", "动机", 1.0),
            ("学习能力强", "特质", 1.0),
            ("沟通表达清晰", "特质", 0.8),
            ("团队合作精神", "自我概念", 0.8),
            ("责任心", "自我概念", 0.8),
        ),
    },
    {
        "role_name": "自动驾驶感知工程师",
        "family_name": "算法",
        "domain_name": "自动驾驶",
        "jd_count": 3,
        "skills": _skills(
            ("目标检测", "知识", 2.0),
            ("语义分割", "知识", 1.5),
            ("多传感器融合", "知识", 2.0),
            ("传感器原理", "知识", 1.0),
            ("深度学习", "知识", 1.5),
            ("YOLO", "技术", 1.5),
            ("PyTorch", "技术", 1.5),
            ("TensorRT", "技术", 1.5),
            ("Python", "技术", 1.5),
            ("C++", "技术", 1.5),
            ("LiDAR点云处理", "技术", 2.0),
            ("OpenCV", "技术", 1.0),
            ("PCL", "技术", 1.0),
            ("模型部署", "技术", 1.5),
            ("硕士及以上学历", "任职条件", 1.5),
            ("计算机相关专业", "任职条件", 1.0),
            ("学习能力强", "特质", 1.0),
            ("抗压能力强", "特质", 1.0),
            ("团队协作精神", "自我概念", 0.8),
            ("强烈的责任心", "自我概念", 0.8),
        ),
    },
    {
        "role_name": "后端开发工程师",
        "family_name": "开发",
        "domain_name": "互联网",
        "jd_count": 5,
        "skills": _skills(
            ("Java", "技术", 2.0),
            ("Spring Boot", "技术", 1.5),
            ("MySQL", "技术", 1.5),
            ("Redis", "技术", 1.5),
            ("消息队列", "技术", 1.0),
            ("分布式系统", "知识", 1.5),
            ("微服务架构", "知识", 1.5),
            ("Linux", "技术", 1.0),
            ("Docker", "技术", 1.0),
            ("Kubernetes", "技术", 0.8),
            ("Python", "技术", 1.0),
            ("计算机相关专业", "任职条件", 1.0),
            ("本科及以上学历", "任职条件", 1.0),
            ("责任心", "自我概念", 0.8),
            ("团队协作", "自我概念", 0.8),
            ("学习能力强", "特质", 1.0),
        ),
    },
    {
        "role_name": "AIGC应用开发工程师",
        "family_name": "开发",
        "domain_name": "AI",
        "jd_count": 3,
        "skills": _skills(
            ("大模型API", "知识", 1.5),
            ("Prompt工程", "知识", 1.5),
            ("RAG", "知识", 2.0),
            ("Agent工作流", "知识", 1.5),
            ("Python", "技术", 2.0),
            ("LangChain", "技术", 1.5),
            ("LangGraph", "技术", 1.0),
            ("向量数据库", "技术", 1.5),
            ("智能客服", "技术", 1.0),
            ("知识库问答", "技术", 1.0),
            ("AI产品设计", "知识", 1.0),
            ("本科及以上学历", "任职条件", 1.0),
            ("计算机相关专业", "任职条件", 1.0),
            ("对AI技术充满热情", "动机", 1.0),
            ("学习能力强", "特质", 1.0),
            ("团队合作精神", "自我概念", 0.8),
        ),
    },
    {
        "role_name": "嵌入式软件工程师",
        "family_name": "开发",
        "domain_name": "IoT",
        "jd_count": 4,
        "skills": _skills(
            ("C语言", "技术", 2.0),
            ("嵌入式Linux", "技术", 1.5),
            ("RTOS", "技术", 1.5),
            ("驱动开发", "技术", 1.5),
            ("ARM架构", "知识", 1.5),
            ("单片机", "技术", 1.5),
            ("I2C", "技术", 0.8),
            ("SPI", "技术", 0.8),
            ("UART", "技术", 0.8),
            ("硬件原理图", "知识", 1.0),
            ("计算机相关专业", "任职条件", 1.0),
            ("本科及以上学历", "任职条件", 1.0),
            ("责任心", "自我概念", 0.8),
            ("团队协作", "自我概念", 0.8),
        ),
    },
    {
        "role_name": "BMS算法工程师",
        "family_name": "算法",
        "domain_name": "新能源",
        "jd_count": 2,
        "skills": _skills(
            ("锂电池", "知识", 2.0),
            ("SOC估算", "知识", 2.0),
            ("卡尔曼滤波", "知识", 1.5),
            ("等效电路模型", "知识", 1.5),
            ("MATLAB", "技术", 1.5),
            ("Simulink", "技术", 1.5),
            ("Python", "技术", 1.0),
            ("C语言", "技术", 1.0),
            ("电池管理系统", "知识", 1.5),
            ("硕士及以上学历", "任职条件", 1.5),
            ("电气相关专业", "任职条件", 1.0),
            ("学习能力强", "特质", 1.0),
            ("责任心", "自我概念", 0.8),
        ),
    },
]


class MemoryRoleStore(RoleStore):
    """内嵌示例 Role 数据的内存实现，不依赖 Neo4j。"""

    def get_all_roles(self) -> List[Dict[str, Any]]:
        return [dict(role) for role in _SAMPLE_ROLES]

    def get_role_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        for role in _SAMPLE_ROLES:
            if role.get("role_name") == name:
                return dict(role)
        return None

