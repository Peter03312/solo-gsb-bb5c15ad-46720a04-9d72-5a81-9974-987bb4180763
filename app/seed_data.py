"""首次启动时写入的种子课程目录（版本 1）。

- prereq_groups：每个内层列表是一组，组内任选一门，各组均须满足；
  例如 CS301 要求 (CS201 或 CS202) 且 MATH103。
- coreqs：同修课程，未修过时必须与该课同学期安排。
- CS999/CS999L 开课季节互斥，用于演示“条件冲突”的报错路径。
"""

SEED_COURSES = [
    {"code": "MATH101", "name": "高等数学I", "credits": 4, "semesters": ["SPRING", "FALL"]},
    {"code": "MATH102", "name": "高等数学II", "credits": 4, "semesters": ["SPRING", "FALL"],
     "prereq_groups": [["MATH101"]]},
    {"code": "MATH103", "name": "线性代数", "credits": 3, "semesters": ["SPRING", "FALL"]},
    {"code": "CS101", "name": "程序设计基础", "credits": 3, "semesters": ["SPRING", "FALL"],
     "coreqs": ["CS101L"]},
    {"code": "CS101L", "name": "程序设计实验", "credits": 1, "semesters": ["SPRING", "FALL"]},
    {"code": "CS201", "name": "数据结构", "credits": 3, "semesters": ["SPRING", "FALL"],
     "prereq_groups": [["CS101"]]},
    {"code": "CS202", "name": "离散数学", "credits": 3, "semesters": ["SPRING", "FALL"],
     "prereq_groups": [["MATH101"]]},
    {"code": "CS301", "name": "算法设计与分析", "credits": 3, "semesters": ["SPRING", "FALL"],
     "prereq_groups": [["CS201", "CS202"], ["MATH103"]]},
    {"code": "CS302", "name": "操作系统", "credits": 3, "semesters": ["FALL"],
     "prereq_groups": [["CS201"]]},
    {"code": "CS303", "name": "计算机网络", "credits": 3, "semesters": ["SPRING"],
     "prereq_groups": [["CS201"]]},
    {"code": "CS401", "name": "编译原理", "credits": 3, "semesters": ["FALL"],
     "prereq_groups": [["CS301"]]},
    {"code": "CS402", "name": "机器学习", "credits": 3, "semesters": ["SPRING"],
     "prereq_groups": [["CS301"], ["MATH102"]]},
    {"code": "PHYS201", "name": "大学物理", "credits": 3, "semesters": ["SPRING"],
     "coreqs": ["PHYS201L"]},
    {"code": "PHYS201L", "name": "物理实验", "credits": 1, "semesters": ["SPRING"]},
    {"code": "CS999", "name": "专题研讨", "credits": 2, "semesters": ["SPRING"],
     "coreqs": ["CS999L"]},
    {"code": "CS999L", "name": "专题研讨实验", "credits": 2, "semesters": ["FALL"]},
]
