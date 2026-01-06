import re
from typing import List, Dict, Any, Set


class FeatureExtractor:
    def __init__(self, rules_config: Dict[str, Any] = None):
        """
        初始化特征提取器
        :param rules_config: 字典格式的配置，通常从 yaml 加载
        """
        self.rules = rules_config or {}

        # --- 1. 加载配置或使用默认正则 ---

        # 时间字段：命中 _time/_date 等，且排除 _by
        self.re_time = re.compile(
            self.rules.get("time", {}).get("include_regex", r".*(_time|_date|dt|gmt)$"),
            re.IGNORECASE
        )
        self.re_time_exclude = re.compile(
            self.rules.get("time", {}).get("exclude_regex", r".*(_by)$"),
            re.IGNORECASE
        )

        # 指标字段：amount, price, qty 等
        self.re_metric = re.compile(
            self.rules.get("metric", {}).get("include_regex",
                                             r".*(amount|price|fee|qty|num|count|duration|balance|total|gmv|revenue).*"),
            re.IGNORECASE
        )

        # 维度字段：status, type, region 等
        self.re_dim = re.compile(
            self.rules.get("dimension", {}).get("include_regex",
                                                r".*(status|type|category|channel|region|city|province|source|mode|flag|tier).*"),
            re.IGNORECASE
        )

        # 外键：_id, code 等
        self.re_join = re.compile(
            self.rules.get("join_keys", {}).get("regex", r".*(_id|code|no)$"),
            re.IGNORECASE
        )

        # 外键白名单 (直接匹配)
        self.join_whitelist = set(
            self.rules.get("join_keys", {}).get("whitelist", ["uid", "uuid", "openid", "member_id"]))

        # 外键黑名单 (排除 id, row_id 等)
        self.join_blacklist = set(self.rules.get("join_keys", {}).get("blacklist", ["id", "row_id"]))

    def infer(self, columns: List[Dict]) -> Dict[str, List[str]]:
        """
        根据列信息推断特征
        :param columns: [{"name": "...", "type": "..."}, ...]
        :return: {"time_cols": [...], ...}
        """
        feats = {
            "time_cols": [],
            "metric_cols": [],
            "dimension_cols": [],
            "join_keys": [],
            "filter_cols": []
        }

        for col in columns:
            name = col["name"].lower()
            ctype = col["type"].lower()

            # 1. Time Cols
            # 逻辑：(类型是时间 OR 名字像时间) AND (名字不像 create_by)
            is_time_type = any(t in ctype for t in ["date", "datetime", "timestamp"])
            if (is_time_type or self.re_time.match(name)) and not self.re_time_exclude.match(name):
                feats["time_cols"].append(col["name"])

            # 2. Metric Cols
            # 逻辑：数值类型 AND 名字像指标 AND 不是ID
            is_numeric = any(t in ctype for t in ["int", "decimal", "double", "float", "numeric", "number"])
            if is_numeric and self.re_metric.match(name) and not name.endswith("_id"):
                feats["metric_cols"].append(col["name"])

            # 3. Join Keys
            # 逻辑：(正则命中 OR 在白名单) AND (不在黑名单)
            if name not in self.join_blacklist:
                if self.re_join.match(name) or name in self.join_whitelist:
                    feats["join_keys"].append(col["name"])

            # 4. Dimension Cols
            # 逻辑：正则命中
            if self.re_dim.match(name):
                feats["dimension_cols"].append(col["name"])
                # 默认维度也可作为 filter
                feats["filter_cols"].append(col["name"])

        return feats