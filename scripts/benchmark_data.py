BENCHMARK_CASES = [
    # ==========================
    # 🟢 基础能力
    # ==========================
    {
        "q": "查询所有订单的支付总金额",
        "expected": ["t_pay_flow"], # 支付金额查流水是没问题的
        "type": "基础-交易"
    },
    {
        "q": "统计最近一个月的订单量",
        "expected": ["t_order"],
        "type": "基础-交易"
    },
    {
        "q": "查看用户 ID 为 10086 的手机号和注册时间",
        "expected": ["u_user_base"], # 查具体个人信息，还是基表准
        "type": "基础-用户"
    },
    {
        "q": "查询SkuID为888的库存剩余数量",
        "expected": ["scm_stock"], # 只要匹配前缀即可 (scm_stock_check/scm_stock_sum)
        "type": "基础-库存"
    },
    {
        "q": "列出所有正在进行的大促活动",
        "expected": ["mkt_activity_main"],
        "type": "基础-营销"
    },

    # ==========================
    # 🔵 进阶：分表与日志
    # ==========================
    {
        "q": "最近两周API接口的平均响应时间",
        "expected": ["log_api_access"],
        "type": "进阶-分表"
    },
    {
        "q": "查看昨天的系统报错堆栈",
        "expected": ["log_err_report"],
        "type": "进阶-分表"
    },
    {
        "q": "统计上个月用户的登录频次",
        "expected": ["u_login_log"],
        "type": "进阶-分表"
    },

    # ==========================
    # 🔴 困难：跨库 JOIN (修正版)
    # ==========================
    {
        # 修正：地区分析，user_dim 比 u_user_base 更好
        "q": "查看北京地区用户的订单消费总额",
        "expected": ["t_order", "user_dim"],
        "type": "困难-跨库"
    },
    {
        # 修正：购买行为分析，user_dim 或 u_user_base 都可以
        "q": "统计购买过'小米手机'的用户的平均等级",
        "expected": ["t_order_item", "user_dim", "u_level_def"],
        "type": "困难-跨库"
    },
    {
        "q": "查看由于'质量问题'退货的供应商分布",
        "expected": ["scm_purchase_return", "scm_supplier_base"],
        "type": "困难-跨库"
    },
    {
        # 修正：直播+新用户。'mkt_live_goods' (带货) 比 'room' 更容易被关联
        "q": "统计直播带货活动带来的新用户注册量",
        "expected": ["mkt_live_goods", "user_dim"],
        "type": "困难-跨库"
    },

    # ==========================
    # 🟣 语义与黑话
    # ==========================
    {
        # 修正：GMV = 订单金额，不强制要求支付流水
        "q": "查看全公司的 GMV (商品交易总额)",
        "expected": ["t_order"],
        "type": "语义-黑话"
    },
    {
        # 客单价 = 订单明细 or 订单主表
        "q": "分析客单价最高的前10个商品",
        "expected": ["t_order_item"],
        "type": "语义-计算"
    },
    {
        "q": "查看进销存流水记录",
        "expected": ["scm_stock"], # 模糊匹配 scm_stock_*
        "type": "语义-集合"
    },

    # ==========================
    # 🟡 抗干扰
    # ==========================
    {
        "q": "查询运费模板的计费方式",
        "expected": ["t_freight_template"],
        "type": "抗噪"
    },
    {
        "q": "查看优惠券的发放记录",
        "expected": ["mkt_coupon_send_log"],
        "type": "抗噪"
    },

    # ==========================
    # ⚫ 熔断 (期望结果为空)
    # ==========================
    {
        "q": "今天天气怎么样",
        "expected": [],
        "type": "熔断"
    },
    {
        "q": "帮我写一首关于数据库的诗",
        "expected": [],
        "type": "熔断"
    },
    {
        "q": "查询员工工资表",
        "expected": [],
        "type": "熔断"
    }
]