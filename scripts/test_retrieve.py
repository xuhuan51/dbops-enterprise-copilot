import numpy as np
from pymilvus import connections, Collection
from sentence_transformers import SentenceTransformer

connections.connect(host="127.0.0.1", port="19530")
col = Collection("schema_catalog")
col.load()

model = SentenceTransformer("BAAI/bge-m3")
q = "近30天订单金额趋势"
qvec = model.encode([q], normalize_embeddings=True).astype(np.float32)

res = col.search(
    data=qvec.tolist(),
    anns_field="vector",
    param={"metric_type": "IP", "params": {"ef": 128}},
    limit=5,
    output_fields=["full_name", "domain"]
)

for hit in res[0]:
    print(hit.entity.get("full_name"), hit.entity.get("domain"), hit.score)
