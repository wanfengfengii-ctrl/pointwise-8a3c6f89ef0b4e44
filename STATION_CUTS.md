# 站批交班截面

接入员收到的站批可以乱序。日结员用各站交班确认的位置组合成一版日结。后续站批可继续到仓，旧向量仍可选用。接收账继续按日内封账顺位收到增减额。

## 入口

| 调用 | 输入和返回 |
| --- | --- |
| `intake.batches.register_stations(local, day, stations)` | 为没有 V1 批次和定稿的日期登记固定站组，返回 `None`。`stations` 是非空列表或元组，元素为互不重复的站名。对同日重复登记相同站集成功，换序等价。改站集或给已有普通日业务记录的日期登记时抛 `ConflictError`。 |
| `intake.batches.receive_station_batch(local, day, station, position, batch_id, kind, rows)` | `station` 必须在当日站组内，`position` 为正整数。`kind` 和 `rows` 使用 README 的 V1 批种与有序三元组格式，每行的站名须等于 `station`。返回对象含 `batch_id`、`state`、`applied_through`、`blocked_position`、`blocked_by`。 |
| `close.finalize.seal_station_cut(local, day, seal_id, positions)` | `positions` 是 `{站名: 非负整数位置}` 的字典，键恰好覆盖登记站组。返回含 `revision` 和 `positions` 的对象，`revision` 使用既有 `Revision`，`positions` 为固定向量的字典。 |
| `close.finalize.lookup_station_cut(local, seal_id)` | 按号读取同一形状的已封站位截面。未知号或普通定稿返回 `None`。 |

日期、封账号、站名、行名、整数升及批种沿用 README。布尔值不能用作位置。输入格式错误、站名不在登记范围或向量键不匹配时抛 `InputError`。新收件和站位封账要求日期已登记。普通 `apply`、`seal` 对站批日抛 `ConflictError`。批号和封账号各自沿用全局身份范围，跨日或跨模式占用也须检查。重复封账号沿用 V1 的拒绝规则。已有库继续用 `initialize_workspace` 初始化或重复初始化，公开操作自行管理事务。

## 收件与生效

每站的已应用位置初始为 0。收件成功后，从本站下一位置按顺序处理已收件的完整批次，直到遇到第一个缺位或失效位。首录要求各行身份尚未占用，勘误要求各行目标当时已经存在。依这些业务规则失效的批次整批不生效，本站停在此前成功位置，后续位置可收件待办，其他站照常处理。失效位置保留原件身份，不提供换件或跳过流程。空批和同值勘误合法，整批成功即推进位置。

收件结果的 `state` 为该件在本次调用结束时的 `pending`、`applied` 或 `invalid`。`applied_through` 是本站连续已生效位置，`blocked_position` 为其后一位，`blocked_by` 为 `missing` 或 `invalid`。同批号重投比对日期、站、位置、批种及完整原序行，匹配时返回该件及本站当前状态，原件清理后也能重投。异参占用批号或另一批号占用同日同站位置时抛 `ConflictError`。业务失效属于收件裁决，可以持久返回。输入错误、身份冲突和数据库异常不得留下部分收件或推进。接纳补件时已等候的批次可在同一次调用中生效，提交失败后可重试。

## 定稿、报送及清理

向量位置 0 取该站空账，正位置取该站完成至该位置时具名行的精确整数升合计。向量任一位置尚未完整生效时抛 `ConflictError`。向量可选择比当前前沿或此前定稿更早的位置。实际向量、总量和日内定稿顺位一同提交，失败无定稿。现有 `audit.lookup.lookup` 仍提供定稿及回执，站位覆盖通过 `lookup_station_cut` 读取。V2 的覆盖依据是固定向量，V1 兼容的 `Revision.source_watermark` 字段不定义 V2 覆盖。现有 `advance` 为每次成功定稿按原顺位报净差，允许零额或负额。

继续使用 `audit.purge.purge` 和 `purge_history`。V2 批仍须早于截止日，且已有成功站位定稿实际覆盖该站的该位置，才可清除该批的原始批载荷和原行。清理登记须指向实际覆盖该批的一份定稿，可以选任一符合条件的历史定稿。较晚定稿选择更早的位置时，先前实际覆盖仍然成立。待办、失效和从未被定稿覆盖的批不得清理。清理后保留身份重投依据及可供任意已应用前缀取值的计量依据。重开后可首次按旧向量定稿，既有定稿和回执仍可按原号领取。

## 联合样例

`samples/station_cuts.json` 的 `batches` 以批号索引，每个对象的字段可作为 `receive_station_batch` 的同名参数。先登记 `stations`，按 `arrival_before_gap_fill` 收件。E3 返回 `pending`，东站停在 1 并报告缺位 2。尝试 `blocked_cut` 应拒绝，再签 `first_seal`。重开工作账，收 `gap_fill`，重投 E3 应返回 `applied`，东站现已应用至 3。

此时按样例 `cutoff` 清理，`first_purge` 的键为本次应清批号，值为可记载的覆盖号。签 `more_seals` 后再次清理，核对 `second_purge`。重开后首次签 `seal_after_purge`，并用原报送和核账接口查 `posting_deltas`、余额及各版原凭据。最后按 `after_invalid` 收件并检查两站状态，成批拒绝 E4 中的两行，按 `valid_cut` 签账并拒绝 `invalid_cut`。可用原 `tests.support.rejected_commit` 分别核对补 E2 和定稿提交失败后的状态与重试。

`.pointwise-runtime.json` 保留现有运行入口。把站批业务核验接入 `verify`，把样例里的补件、旧截面及清理后续封接入 `demo`，两处继续运行原有业务检查和展示。核验不符时保留失败退出码。
