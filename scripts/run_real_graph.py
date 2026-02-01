import pickle
import sys
import os
import json
import time
import collections
import networkx as nx

# ==========================================
# 1. 环境准备
# ==========================================
# 确保能导入 app 模块
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

try:
    from app.modules.retrieval.graph.builder import SchemaGraphBuilder
    from app.core.config import settings

    print("✅ 成功导入 SchemaGraphBuilder")
except ImportError as e:
    print(f"❌ 导入失败: {e}")
    print("请确保你在项目根目录下运行此脚本，且 app/ 目录结构正确。")
    sys.exit(1)


def run_real_test():
    # 1. 定位真实数据文件
    # 假设你的目录结构是标准 BIRD 结构
    REAL_PATH = os.path.join(os.path.dirname(__file__), "../data/bird/metadata/schema_catalog.json")

    if not os.path.exists(REAL_PATH):
        print(f"\n❌ 致命错误：找不到文件 {REAL_PATH}")
        print("请先运行 build_bird_catalog 生成 schema_catalog.json！")
        return

    print(f"\n📂 正在读取真实数据: {REAL_PATH} ...")
    start_load = time.time()
    with open(REAL_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(f"   - 数据加载耗时: {time.time() - start_load:.2f}s")
    print(f"   - 包含列/字段总数: {len(data)}")

    # 2. 开始构建 (计算核心)
    print("\n🕸️ [V3 Builder] 开始全量计算 (Jaccard + 规则推导)...")
    print("   (如果数据量大，这一步可能需要几秒到十几秒)")

    start_build = time.time()

    # 初始化 Builder
    builder = SchemaGraphBuilder(data)
    # 执行构建
    graphs = builder.build_all()

    elapsed = time.time() - start_build
    print(f"✅ 构建完成！总耗时: {elapsed:.2f}s")
    print(f"📚 处理数据库数量: {len(graphs)}")

    # 3. 深度统计分析
    total_edges = 0
    edge_stats = collections.defaultdict(int)
    sota_edges = []  # 专门收集 SOTA 算法发现的边

    print("\n📊 边类型统计 (Edge Type Statistics):")
    print("=" * 60)

    for db_id, G in graphs.items():
        total_edges += G.number_of_edges()

        for u, v, key, attr in G.edges(keys=True, data=True):
            etype = attr.get('type', 'UNKNOWN')
            edge_stats[etype] += 1

            # 收集一些有趣的 SOTA 边作为展示
            if etype in ["CONTENT_STRONG", "INJECTION", "SEMANTIC"]:
                sota_edges.append({
                    "db": db_id,
                    "u": u, "v": v,
                    "type": etype,
                    "on": attr.get("on")
                })

    # 打印统计表
    # 排序打印，让结果好看点
    sorted_stats = sorted(edge_stats.items(), key=lambda x: x[1], reverse=True)
    for etype, count in sorted_stats:
        # 给不同类型加点备注
        note = ""
        if etype == "EXPLICIT":
            note = "(原始外键)"
        elif etype == "INJECTION":
            note = "(表名推导 ✅)"
        elif etype == "CONTENT_STRONG":
            note = "(数据重叠 >90% 🔥)"
        elif etype == "CONTENT_WEAK":
            note = "(数据重叠 >60% ⚠️)"
        elif etype == "SAME_NAME":
            note = "(同名猜测)"
        elif etype == "SEMANTIC":
            note = "(语义向量 🧠)"

        print(f"{etype:<20} : {count:>5} 条  {note}")

    print("-" * 60)
    print(f"总计边数 (Total Edges) : {total_edges}")

    # 4. 展示 SOTA 成果
    print("\n🎉 [SOTA 成果展示] 随机抽取 5 条我们自动发现的‘隐形关系’：")
    if sota_edges:
        import random
        # 随机抽 5 条，如果不足 5 条就全显示
        samples = random.sample(sota_edges, min(5, len(sota_edges)))
        for s in samples:
            print(f"   [{s['type']}] {s['db']} :: {s['on']}")
    else:
        print("   (暂未发现隐形关系，可能是因为数据全是显式外键，或者样本重叠率不足)")

    # 5. 检查孤立节点 (可选)
    # 很多 Text-to-SQL 失败是因为表是孤立的，连不上
    isolated_dbs = [db for db, G in graphs.items() if G.number_of_edges() == 0]
    if isolated_dbs:
        print(f"\n⚠️ 警告：以下 {len(isolated_dbs)} 个数据库没有任何连接关系 (孤岛):")
        print(f"   {isolated_dbs[:5]} ...")

    #== == == == == == == == == == == == == == == == == == == == ==
    # 🔥 新增：保存到硬盘 (持久化)
    # ==========================================
    OUTPUT_PATH = os.path.join(os.path.dirname(REAL_PATH), "graph_cache.pkl")

    print(f"\n💾 正在保存图谱到文件: {OUTPUT_PATH} ...")
    try:
        with open(OUTPUT_PATH, "wb") as f:
            pickle.dump(graphs, f)
        print(f"✅ 保存成功！文件大小: {os.path.getsize(OUTPUT_PATH) / 1024:.2f} KB")
        print("   (下次启动服务时，可以直接加载这个文件，不用再重新计算了)")
    except Exception as e:
        print(f"❌ 保存失败: {e}")


if __name__ == "__main__":
    run_real_test()