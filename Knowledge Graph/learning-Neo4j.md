#### 官方学习手册
https://neo4j.ac.cn/docs/getting-started/

# Neo4j + Cypher 入门实战

> 目标：不背语法，用一个“人物-城市-关系”的小案例，快速学会 **增删改查**。  
> 节点（Node）：`Person`（人）、`Location`（城市）  
> 关系（Relationship）：`FRIENDS`（朋友）、`MARRIED`（夫妻）、`BORN_IN`（出生地）

---

## 0. 重点知识

- **MATCH**：查找/匹配图中的节点与关系  
- **CREATE**：创建节点/关系  
- **MERGE**：如果不存在就创建（更适合避免重复）  
- **SET / REMOVE**：修改/删除属性  
- **DELETE / DETACH DELETE**：删除节点与关系  
- **关系方向**：`(a)-[:TYPE]->(b)` 有方向；`(a)-[:TYPE]-(b)` 无方向匹配

---

## 1. 清空数据库（从零开始）

⚠️ 会删除当前库里的所有节点与关系。

```cypher
MATCH (n) DETACH DELETE n
```

解释：
- `MATCH (n)`：匹配所有节点（`( )` 表示节点）
- `DETACH DELETE`：先删掉节点的所有关系，再删节点（否则普通 `DELETE` 可能报错）

---

## 2. 创建人物节点 Person

创建一个人：

```cypher
CREATE (n:Person {name:'John'}) RETURN n
```

解释：
- `:Person` 是 **标签**（节点类型）
- `{name:'John'}` 是 **属性**（类似 Python dict）
- `RETURN n` 返回创建结果，方便在 Neo4j Browser 里可视化

再创建更多人物：

```cypher
CREATE (:Person {name:'Sally'});
CREATE (:Person {name:'Steve'});
CREATE (:Person {name:'Mike'});
CREATE (:Person {name:'Liz'});
CREATE (:Person {name:'Shawn'});
```

> 小技巧：这里不需要每次都写 `RETURN`，批量创建更清爽。

---

## 3. 创建城市节点 Location

```cypher
CREATE (:Location {city:'Miami', state:'FL'});
CREATE (:Location {city:'Boston', state:'MA'});
CREATE (:Location {city:'Lynn', state:'MA'});
CREATE (:Location {city:'Portland', state:'ME'});
CREATE (:Location {city:'San Francisco', state:'CA'});
```

解释：
- 城市节点标签是 `Location`
- 属性有 `city`、`state`

---

## 4. 创建人物之间的关系（朋友/结婚）

### 4.1 创建朋友关系（有方向）

```cypher
MATCH (a:Person {name:'Liz'}),
      (b:Person {name:'Mike'})
MERGE (a)-[:FRIENDS]->(b)
```

解释：
- `MATCH ...` 先找到两个人
- `MERGE`：如果这条关系不存在就创建（避免重复）
- `->` 表示方向：从 `Liz` 指向 `Mike`

### 4.2 关系也可以有属性（例如：成为朋友的年份）

```cypher
MATCH (a:Person {name:'Shawn'}),
      (b:Person {name:'Sally'})
MERGE (a)-[:FRIENDS {since:2001}]->(b)
```

关系属性同样用 `{}`，例如 `since:2001`。

### 4.3 更多人物关系

```cypher
MATCH (a:Person {name:'Shawn'}), (b:Person {name:'John'})
MERGE (a)-[:FRIENDS {since:2012}]->(b);

MATCH (a:Person {name:'Mike'}), (b:Person {name:'Shawn'})
MERGE (a)-[:FRIENDS {since:2006}]->(b);

MATCH (a:Person {name:'Sally'}), (b:Person {name:'Steve'})
MERGE (a)-[:FRIENDS {since:2006}]->(b);

MATCH (a:Person {name:'Liz'}), (b:Person {name:'John'})
MERGE (a)-[:MARRIED {since:1998}]->(b);
```

---

## 5. 创建人物与城市的关系（出生地）

### 5.1 给 John 添加出生地（带出生年份）

```cypher
MATCH (a:Person {name:'John'}), (b:Location {city:'Boston'})
MERGE (a)-[:BORN_IN {year:1978}]->(b)
```

### 5.2 更多人的出生地

```cypher
MATCH (a:Person {name:'Liz'}),   (b:Location {city:'Boston'})
MERGE (a)-[:BORN_IN {year:1981}]->(b);

MATCH (a:Person {name:'Mike'}),  (b:Location {city:'San Francisco'})
MERGE (a)-[:BORN_IN {year:1960}]->(b);

MATCH (a:Person {name:'Shawn'}), (b:Location {city:'Miami'})
MERGE (a)-[:BORN_IN {year:1960}]->(b);

MATCH (a:Person {name:'Steve'}), (b:Location {city:'Lynn'})
MERGE (a)-[:BORN_IN {year:1970}]->(b);
```

---

## 6. 开始查询（Query）

### 6.1 查询所有在 Boston 出生的人

```cypher
MATCH (a:Person)-[:BORN_IN]->(b:Location {city:'Boston'})
RETURN a, b
```

### 6.2 查询所有“对外”有关系的节点（有出边）

```cypher
MATCH (a)-->() RETURN a
```

说明：
- `-->()` 表示“从 a 出发，有一条向外的关系”
- 你的城市节点通常只有“被指向”，因此可能不会出现在结果里

### 6.3 查询所有“有任何关系”的节点（不管方向）

```cypher
MATCH (a)--() RETURN a
```

### 6.4 查询所有对外关系的节点 + 关系类型

```cypher
MATCH (a)-[r]->()
RETURN a.name, type(r)
```

- `[r]` 表示关系变量
- `type(r)` 返回关系类型（如 FRIENDS / MARRIED / BORN_IN）

### 6.5 查询所有有结婚关系的人

```cypher
MATCH (n)-[:MARRIED]-() RETURN n
```

这里用 `-[:MARRIED]-` 表示无方向匹配（夫妻关系你可能不关心方向）。

---

## 7. 创建时顺便建关系（一步到位）

```cypher
CREATE (a:Person {name:'Todd'})-[r:FRIENDS]->(b:Person {name:'Carlos'})
```

---

## 8. 查“朋友的朋友”（2 跳关系）

以 Mike 为例：找 `Mike` 的朋友的朋友。

```cypher
MATCH (a:Person {name:'Mike'})-[r1:FRIENDS]-()-[r2:FRIENDS]-(friend_of_a_friend)
RETURN friend_of_a_friend.name AS fofName
```

解释：
- `-[FRIENDS]-()`：中间这个 `()` 表示“任意节点”
- 两段 `FRIENDS` 连起来就是两跳（2-hop）

---

## 9. 修改 / 删除属性（Update）

### 9.1 增加或修改节点属性：SET

```cypher
MATCH (a:Person {name:'Liz'})   SET a.age = 34;
MATCH (a:Person {name:'Shawn'}) SET a.age = 32;
MATCH (a:Person {name:'John'})  SET a.age = 44;
MATCH (a:Person {name:'Mike'})  SET a.age = 25;
```

### 9.2 删除属性：REMOVE

```cypher
MATCH (a:Person {name:'Mike'}) SET a.test = 'test';
MATCH (a:Person {name:'Mike'}) REMOVE a.test;
```

---

## 10. 删除节点 / 删除带关系的节点（Delete）

### 10.1 删除一个没有关系的节点

```cypher
MATCH (a:Location {city:'Portland'}) DELETE a
```

> 如果它还有关系，直接 `DELETE` 会报错。

### 10.2 删除“带关系”的节点（同时删关系）

```cypher
MATCH (a:Person {name:'Todd'})-[rel]-(b:Person)
DELETE a, b, rel
```

或者更通用的“强制删除节点及其关系”：

```cypher
MATCH (a:Person {name:'Todd'}) DETACH DELETE a
```

---

## 11. 一句总结（记忆口诀）

- **查**：`MATCH`  
- **建**：`CREATE`（一定创建）、`MERGE`（不存在才创建）  
- **改**：`SET`  
- **删属性**：`REMOVE`  
- **删节点**：`DELETE`（无关系）、`DETACH DELETE`（有关系也能删）  
- **方向**：`->` 有方向；`- -` 无方向匹配

