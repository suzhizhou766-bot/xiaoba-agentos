# 用户本人在 zsh 中 source 本文件；Codex 不执行此配置写入步骤。
function _xiaoba_agy_vpn_enable() {
  emulate -L zsh
  local agy_vpn_profile="$HOME/.zshrc"
  local agy_vpn_source_line="source '/Users/suzhizhou/Documents/私人助理/工具/AGY-VPN/启用当前终端.zsh'"
  if [[ -L "$agy_vpn_profile" ]]; then
    print -u2 '未修改：.zshrc 是符号链接，请人工确认其目标。'
    return 1
  fi
  if [[ -e "$agy_vpn_profile" && ! -f "$agy_vpn_profile" ]]; then
    print -u2 '未修改：.zshrc 不是普通文件。'
    return 1
  fi
  if [[ -f "$agy_vpn_profile" ]] && ! /bin/zsh -n "$agy_vpn_profile"; then
    print -u2 '未修改：现有 .zshrc 语法检查未通过。'
    return 1
  fi
  if [[ ! -f "$agy_vpn_profile" ]] || ! /usr/bin/grep -Fqx -- "$agy_vpn_source_line" "$agy_vpn_profile"; then
    printf '\n%s\n' "$agy_vpn_source_line" >> "$agy_vpn_profile" || return 1
  fi
  source '/Users/suzhizhou/Documents/私人助理/工具/AGY-VPN/启用当前终端.zsh'
  print '已启用：当前及以后新开的交互式 zsh 终端中，agy（CLI）和 antigravity（桌面端）通过系统的本机 VPN 代理启动。'
  print '其他命令的代理环境未更改；VPN 代理不可用时，agy 会停止而不回退直连。'
}
if _xiaoba_agy_vpn_enable; then
  unfunction _xiaoba_agy_vpn_enable
else
  unfunction _xiaoba_agy_vpn_enable
  return 1
fi
