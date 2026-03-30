"""
Graph-RAG 检索器 — Neo4j 知识图谱检索

WHY Graph-RAG 是向量检索的必要补充：
- 向量检索擅长语义相似度（"HDFS 容量不足"能找到类似问题）
- 但向量检索不理解关系（"NN 挂了会影响哪些下游？"需要图遍历）
- 知识图谱记录组件间的拓扑关系和因果链
- Graph-RAG + Vector-RAG 融合 → 既相似又相关

图数据模型（参考 13-Graph-RAG.md）：
  Node 类型: Component, Incident, SOP, Person
  Edge 类型: DEPENDS_ON, CAUSED_BY, RESOLVED_BY, SIMILAR_TO

双模式设计：
1. 生产模式：neo4j async driver + Cypher 查询
2. Mock 模式：内置拓扑知识 + 关键词匹配
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aiops.core.config import get_settings
from aiops.core.logging import get_logger
from aiops.rag.types import RetrievedDocument, RetrievalQuery

logger = get_logger(__name__)


@dataclass
class GraphNode:
    """知识图谱节点."""
    id: str
    label: str          # Component / Incident / SOP / Person
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphEdge:
    """知识图谱边."""
    source_id: str
    target_id: str
    relation: str       # DEPENDS_ON / CAUSED_BY / RESOLVED_BY / SIMILAR_TO
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphRAGResult:
    """图检索结果."""
    documents: list[RetrievedDocument]
    subgraph: list[dict[str, Any]]  # 命中的子图（节点+边）
    paths: list[list[str]]          # 因果路径


class GraphRAGRetriever:
    """
    知识图谱 RAG 检索器 — 双模式（生产 + Mock）.

    检索策略：
    1. 实体识别：从查询中提取组件名/错误码/操作类型
    2. 子图遍历：以实体为起点，N-hop 遍历关联节点
    3. 路径查找：查找因果链（A CAUSED_BY B CAUSED_BY C）
    4. 文档关联：将图节点关联到 SOP 文档

    生产模式：neo4j async driver + Cypher
    Mock 模式：内置拓扑知识（开发/测试/面试演示）

    WHY 不直接用 Neo4j GDS 的图算法：
    - GDS 是重量级组件，开发阶段用 Cypher 查询足够
    - 实际查询模式很固定（N-hop + 最短路径），不需要 PageRank 等复杂算法
    - 保持轻量，生产环境按需启用 GDS
    """

    # 内置组件拓扑知识（Mock）
    COMPONENT_TOPOLOGY: dict[str, list[str]] = {
        "hdfs-namenode": ["hdfs-datanode", "zookeeper", "yarn-resourcemanager"],
        "hdfs-datanode": ["hdfs-namenode"],
        "yarn-resourcemanager": ["yarn-nodemanager", "hdfs-namenode", "zookeeper"],
        "yarn-nodemanager": ["yarn-resourcemanager"],
        "kafka-broker": ["zookeeper", "kafka-consumer"],
        "kafka-consumer": ["kafka-broker"],
        "es-master": ["es-data", "es-client"],
        "es-data": ["es-master"],
        "zookeeper": [],  # 被很多组件依赖
    }

    # 常见因果链（Mock）
    CAUSAL_CHAINS: list[dict[str, Any]] = [
        {
            "id": "chain-1",
            "trigger": "namenode_oom",
            "chain": ["NN 堆内存不足", "Full GC 频繁", "RPC 处理阻塞", "DN 心跳超时", "Block 汇报延迟"],
            "components": ["hdfs-namenode", "hdfs-datanode"],
            "resolution": "增大 NN 堆内存 + 检查 Block 数量",
        },
        {
            "id": "chain-2",
            "trigger": "kafka_lag_spike",
            "chain": ["Consumer 处理变慢", "Lag 堆积", "Topic 数据过期", "数据丢失风险"],
            "components": ["kafka-consumer", "kafka-broker"],
            "resolution": "扩容 Consumer 实例 + 调整 retention",
        },
        {
            "id": "chain-3",
            "trigger": "es_cluster_red",
            "chain": ["主分片丢失", "副本无法提升", "索引不可写入", "日志采集阻塞"],
            "components": ["es-master", "es-data"],
            "resolution": "检查节点状态 + 手动 reroute 分片",
        },
        {
            "id": "chain-4",
            "trigger": "yarn_queue_full",
            "chain": ["队列资源耗尽", "新任务排队", "SLA 超时", "业务投诉"],
            "components": ["yarn-resourcemanager", "yarn-nodemanager"],
            "resolution": "调整队列配额 + Kill 低优先级任务",
        },
        {
            "id": "chain-5",
            "trigger": "zk_session_timeout",
            "chain": ["ZK Session 超时", "Leader 重选举", "依赖 ZK 的组件闪断", "服务中断"],
            "components": ["zookeeper", "hdfs-namenode", "kafka-broker"],
            "resolution": "检查 ZK 服务器负载 + 增大 session timeout",
        },
    ]

    # Mock 历史故障案例
    HISTORICAL_INCIDENTS: list[dict[str, Any]] = [
        {
            "id": "inc-001",
            "title": "NameNode OOM 导致 HDFS 不可用",
            "component": "hdfs-namenode",
            "root_cause": "Block 数量超过 2 亿，NN 堆内存 32GB 不够",
            "resolution": "增大 NN 堆到 64GB，启用联邦 HDFS",
            "tags": ["hdfs", "oom", "namenode"],
        },
        {
            "id": "inc-002",
            "title": "Kafka Consumer Lag 告警",
            "component": "kafka-consumer",
            "root_cause": "Consumer 节点 GC 导致处理变慢",
            "resolution": "优化 Consumer JVM 参数，增加 partition 数",
            "tags": ["kafka", "lag", "gc"],
        },
        {
            "id": "inc-003",
            "title": "ES 集群 Red 状态",
            "component": "es-master",
            "root_cause": "数据节点磁盘满，分片无法分配",
            "resolution": "清理历史索引，扩容磁盘",
            "tags": ["es", "red", "disk"],
        },
    ]

    def __init__(self, neo4j_uri: str = "") -> None:
        settings = get_settings()
        self._neo4j_uri = neo4j_uri or settings.rag.neo4j_uri
        self._neo4j_user = settings.rag.neo4j_user
        self._neo4j_password = settings.rag.neo4j_password.get_secret_value()

        # 延迟初始化
        self._driver: Any = None
        self._use_mock = not self._neo4j_uri
        self._connected = False

    async def retrieve(
        self,
        query: RetrievalQuery,
    ) -> GraphRAGResult:
        """
        图检索.

        1. 从查询中提取组件
        2. 查找关联因果链
        3. 查找历史案例
        4. 返回融合结果
        """
        if self._use_mock:
            return await self._mock_retrieve(query)

        try:
            return await self._neo4j_retrieve(query)
        except Exception as e:
            logger.warning(
                "graph_rag_neo4j_failed_fallback_mock",
                error=str(e),
                query=query.text[:80],
            )
            return await self._mock_retrieve(query)

    async def _ensure_connected(self) -> None:
        """延迟连接 Neo4j."""
        if self._connected:
            return

        try:
            from neo4j import AsyncGraphDatabase

            self._driver = AsyncGraphDatabase.driver(
                self._neo4j_uri,
                auth=(self._neo4j_user, self._neo4j_password) if self._neo4j_password else None,
            )

            # 验证连接
            async with self._driver.session() as session:
                result = await session.run("RETURN 1 AS n")
                await result.single()

            logger.info("neo4j_connected", uri=self._neo4j_uri)
            self._connected = True

        except Exception as e:
            logger.warning("neo4j_connect_failed", error=str(e))
            self._use_mock = True
            if self._driver:
                await self._driver.close()
                self._driver = None

    async def _neo4j_retrieve(self, query: RetrievalQuery) -> GraphRAGResult:
        """
        Neo4j Cypher 查询.

        三步检索：
        1. 实体匹配：全文索引查找 Component/Incident 节点
        2. 子图遍历：从匹配节点出发 2-hop 遍历
        3. 因果链查找：CAUSED_BY 路径
        """
        await self._ensure_connected()

        if self._use_mock:
            return await self._mock_retrieve(query)

        documents: list[RetrievedDocument] = []
        subgraph: list[dict[str, Any]] = []
        paths: list[list[str]] = []

        async with self._driver.session() as session:
            # Step 1: 全文搜索匹配组件和故障案例
            # WHY 用 CONTAINS 而不是全文索引：
            # 全文索引需要预建，CONTAINS 对小数据集足够（<10000 节点）
            cypher_match = """
            MATCH (n)
            WHERE (n:Component OR n:Incident OR n:SOP)
              AND (toLower(n.name) CONTAINS toLower($query)
                   OR toLower(coalesce(n.description, '')) CONTAINS toLower($query))
            RETURN n, labels(n) AS labels
            LIMIT 10
            """
            result = await session.run(cypher_match, query=query.text)
            matched_nodes = []
            async for record in result:
                node = record["n"]
                node_labels = record["labels"]
                matched_nodes.append(node)
                subgraph.append({
                    "node": dict(node),
                    "label": node_labels[0] if node_labels else "Unknown",
                })

            # Step 2: 从匹配节点出发 2-hop 子图遍历
            if matched_nodes:
                node_ids = [n.element_id for n in matched_nodes]
                cypher_subgraph = """
                MATCH (start)-[r*1..2]-(related)
                WHERE elementId(start) IN $node_ids
                RETURN DISTINCT start, r, related, type(r[0]) AS rel_type
                LIMIT 50
                """
                result = await session.run(cypher_subgraph, node_ids=node_ids)
                async for record in result:
                    related = record["related"]
                    rel_type = record["rel_type"]
                    subgraph.append({
                        "edge": f"{dict(record['start'])} → {dict(related)}",
                        "relation": rel_type,
                    })

            # Step 3: 因果链查找
            cypher_causal = """
            MATCH path = (a)-[:CAUSED_BY*1..5]->(b)
            WHERE toLower(a.name) CONTAINS toLower($query)
               OR toLower(b.name) CONTAINS toLower($query)
            RETURN [n IN nodes(path) | n.name] AS causal_path,
                   [n IN nodes(path) | n.description] AS descriptions
            LIMIT 5
            """
            result = await session.run(cypher_causal, query=query.text)
            async for record in result:
                causal_path = record["causal_path"]
                descriptions = record["descriptions"]
                paths.append(causal_path)

                # 将因果链转为文档
                doc = RetrievedDocument(
                    id=f"causal-{'-'.join(causal_path[:3])}",
                    content=(
                        f"因果链: {' → '.join(causal_path)}\n"
                        f"详情: {'; '.join(d for d in descriptions if d)}"
                    ),
                    source="graph_rag:causal_chain",
                    score=0.85,
                    metadata={"type": "causal_chain", "path": causal_path},
                )
                documents.append(doc)

            # Step 4: 关联的 SOP 文档
            cypher_sop = """
            MATCH (n)-[:RESOLVED_BY]->(sop:SOP)
            WHERE toLower(n.name) CONTAINS toLower($query)
            RETURN sop
            LIMIT 5
            """
            result = await session.run(cypher_sop, query=query.text)
            async for record in result:
                sop = record["sop"]
                doc = RetrievedDocument(
                    id=f"sop-{sop.get('id', 'unknown')}",
                    content=sop.get("content", sop.get("description", "")),
                    source=f"graph_rag:sop:{sop.get('name', '')}",
                    score=0.80,
                    metadata={"type": "sop", "name": sop.get("name", "")},
                )
                documents.append(doc)

        logger.info(
            "graph_rag_neo4j_retrieve",
            query=query.text[:100],
            documents=len(documents),
            paths=len(paths),
            subgraph_size=len(subgraph),
        )

        return GraphRAGResult(
            documents=documents,
            subgraph=subgraph,
            paths=paths,
        )

    async def _mock_retrieve(self, query: RetrievalQuery) -> GraphRAGResult:
        """Mock 检索——基于关键词匹配内置知识。"""
        query_lower = query.text.lower()
        documents: list[RetrievedDocument] = []
        subgraph: list[dict[str, Any]] = []
        paths: list[list[str]] = []

        # 1. 匹配因果链
        for chain in self.CAUSAL_CHAINS:
            trigger = chain["trigger"].lower()
            if any(kw in query_lower for kw in trigger.split("_")):
                doc = RetrievedDocument(
                    id=chain["id"],
                    content=(
                        f"因果链: {' → '.join(chain['chain'])}\n"
                        f"涉及组件: {', '.join(chain['components'])}\n"
                        f"建议处理: {chain['resolution']}"
                    ),
                    source="graph_rag:causal_chain",
                    score=0.85,
                    metadata={"type": "causal_chain", "components": chain["components"]},
                )
                documents.append(doc)
                paths.append(chain["chain"])

        # 2. 匹配历史案例
        for incident in self.HISTORICAL_INCIDENTS:
            if any(tag in query_lower for tag in incident["tags"]):
                doc = RetrievedDocument(
                    id=incident["id"],
                    content=(
                        f"历史案例: {incident['title']}\n"
                        f"根因: {incident['root_cause']}\n"
                        f"处理: {incident['resolution']}"
                    ),
                    source="graph_rag:incident",
                    score=0.80,
                    metadata={"type": "incident", "component": incident["component"]},
                )
                documents.append(doc)

        # 3. 构建子图
        matched_components: set[str] = set()
        for doc in documents:
            meta = doc.metadata or {}
            comps = meta.get("components", [])
            if isinstance(comps, list):
                matched_components.update(comps)
            comp = meta.get("component", "")
            if comp:
                matched_components.add(comp)

        for comp in matched_components:
            subgraph.append({"node": comp, "label": "Component"})
            for dep in self.COMPONENT_TOPOLOGY.get(comp, []):
                subgraph.append({
                    "edge": f"{comp} → {dep}",
                    "relation": "DEPENDS_ON",
                })

        logger.info(
            "graph_rag_mock_retrieve",
            query=query.text[:100],
            documents=len(documents),
            paths=len(paths),
            subgraph_nodes=len(matched_components),
        )

        return GraphRAGResult(
            documents=documents,
            subgraph=subgraph,
            paths=paths,
        )

    async def get_component_topology(
        self,
        component: str,
        depth: int = 2,
    ) -> dict[str, Any]:
        """获取组件的 N-hop 拓扑."""
        # 生产模式：Cypher 查询
        if self._connected and self._driver:
            try:
                async with self._driver.session() as session:
                    cypher = """
                    MATCH path = (c:Component {name: $component})-[:DEPENDS_ON*1..$depth]-(related:Component)
                    RETURN c.name AS source,
                           [n IN nodes(path) | n.name] AS path_nodes,
                           [r IN relationships(path) | type(r)] AS relations
                    LIMIT 50
                    """
                    result = await session.run(cypher, component=component, depth=depth)
                    topology: dict[str, list[str]] = {}
                    async for record in result:
                        path_nodes = record["path_nodes"]
                        for i in range(len(path_nodes) - 1):
                            src = path_nodes[i]
                            if src not in topology:
                                topology[src] = []
                            topology[src].append(path_nodes[i + 1])
                    return topology
            except Exception as e:
                logger.warning("neo4j_topology_failed_fallback_mock", error=str(e))

        # Mock fallback
        visited: set[str] = set()
        topology_mock: dict[str, list[str]] = {}

        def _traverse(node: str, current_depth: int) -> None:
            if current_depth > depth or node in visited:
                return
            visited.add(node)
            deps = self.COMPONENT_TOPOLOGY.get(node, [])
            topology_mock[node] = deps
            for dep in deps:
                _traverse(dep, current_depth + 1)

        _traverse(component, 0)
        return topology_mock

    async def find_causal_path(
        self,
        source_component: str,
        target_component: str,
    ) -> list[str] | None:
        """查找两个组件间的因果路径."""
        # 生产模式：Cypher 最短路径
        if self._connected and self._driver:
            try:
                async with self._driver.session() as session:
                    cypher = """
                    MATCH path = shortestPath(
                        (a:Component {name: $source})-[:CAUSED_BY*..10]-(b:Component {name: $target})
                    )
                    RETURN [n IN nodes(path) | n.name] AS causal_path
                    """
                    result = await session.run(
                        cypher,
                        source=source_component,
                        target=target_component,
                    )
                    record = await result.single()
                    if record:
                        return record["causal_path"]
            except Exception as e:
                logger.warning("neo4j_causal_path_failed", error=str(e))

        # Mock fallback
        for chain in self.CAUSAL_CHAINS:
            components = chain["components"]
            if source_component in components and target_component in components:
                return chain["chain"]
        return None

    async def close(self) -> None:
        """关闭 Neo4j 连接."""
        if self._driver:
            try:
                await self._driver.close()
                logger.info("neo4j_driver_closed")
            except Exception as e:
                logger.warning("neo4j_close_error", error=str(e))
            finally:
                self._driver = None
                self._connected = False
