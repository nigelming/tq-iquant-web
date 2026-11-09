// 通达信用户板块代码派生：用户输入的中文名作 block_name，block_code 由名称
// 确定性派生（djb2 → base36 大写 6 位，前缀 CY）——同名同码（幂等重推成立）、
// 异名异码，纯 ASCII 满足通达信板块代码约束。
export function deriveBlockCode(name: string): string {
  let h = 5381
  for (let i = 0; i < name.length; i++) {
    h = (((h << 5) + h) ^ name.charCodeAt(i)) | 0
  }
  const v = ((h >>> 0) % 36 ** 6).toString(36).toUpperCase().padStart(6, '0')
  return `CY${v}`
}
