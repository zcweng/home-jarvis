# 小米 AI 音箱（MDZ-25-DA / S12A）：刷机 + 寄生端点接入清单

> 撰写：2026-10-29 · 目标：把这台音箱变成 Mac 上 home-jarvis 的**耳朵和喇叭**，并**保住远场拾音与 AEC**。
> 结论先行：**不换系统**。做法是"寄生"——保留小米原生固件与音频链路，只注入我们自己的采集/播放组件。
> 状态：🟡 **2026-11-02 归档**——手头实机是 **S12 / 1.74.18**（非本文 S12A），已进 root shell + SSH；S12A 寄生方案在此机型**不成立**。详见 §0.0 与 §16。

---

## 📌 快照：本文档现在怎么用（2026-11-02）

| 你想做的事 | 去哪看 |
| --- | --- |
| 了解手头这台机器的真实环境 | **§0.0**（必读，S12 / 1.74.18 实测） |
| 串口接线 / 进 root shell | §0.0「进入 root shell 的实测流程」 |
| 开 SSH / 开机自启 dropbear | §16.1 / §16.6 |
| S12 音频架构结论（为什么 S12A 方案不成立） | **§16.3 / §16.7 / §16.8** |
| S12A 原始刷机方案（**换机型后才用**） | §4 ~ §9（正文） |
| 下一步待验证 | §16.4 + §16.7「推荐验证路线」 |

**一句话结论**：S12 拿得到 root + SSH，但**不能**用本清单的 `libxaudio_engine.so` 注入方案——那库在 S12 上不存在；采放由 `mipns-xiaomi` 独占，要接 home-jarvis 只能「接管 pns」，代价是丢原厂 AEC/唤醒。

---

## ⚠️ 0.0 本机实测环境（2026-11-01，**动手前必读**）

> 手头这台**不是**本清单的目标机（S12A）。实测为 **S12 / ROM 1.74.18**，分区表、文件系统、镜像大小都与 S12A 不同。
> **本文中所有针对 S12A 的补丁、哈希白名单、mtd 编号、镜像大小、封 OTA 步骤，一律不得套用到这台机器上。**

| 项 | 实测值 |
| --- | --- |
| 主板丝印 | `DKSND-S12-ECHO-AB-20170210`（黑色阻焊） |
| 机型 / HARDWARE | `S12`（`/usr/share/mico/version`、`system.cfg` model=S12） |
| ROM / CHANNEL | `1.74.18` / `release` |
| 构建时间 | `Wed, 06 Dec 2023 14:32:44 +0800`（BUILDTS `1701844364`） |
| GTAG | `commit b9e9b6640c2491c7a77a22612e47790e6c8c0356` |
| U-Boot | `2015.01-g85a2387-dirty (Sep 19 2017 19:00:12)` |
| 内核 | `Linux 3.14.29 aarch64`（`DISTRIB_TARGET=meson/gxl`） |
| 发行版 | OpenWrt Chaos Calmer 15.05.1 |
| hostname | 正常启动 `S12`；failsafe 下 `(none)` |
| 串口设备 | `/dev/tty.usbserial-3120`（偶发枚举为 `-3110`），115200 8N1 |
| 调试口 | 主板 `J28` 3 针；**最右（靠丝印）= GND**，中间/左侧 = TX/RX（与 MTX/RX 交叉） |
| SN 标签 | `14891/980800728` |
| 当前运行根 | `ubi0:rootfs` → **mtd4 = system1**（可写 UBIFS） |

### 分区表（`cat /proc/mtd`）

| dev | size | name | 说明 |
| --- | --- | --- | --- |
| mtd0 | 2 MiB | `nandboot` | |
| mtd1 | 16 MiB | `boot0` | 内核/启动槽 A |
| mtd2 | 16 MiB | `boot1` | 内核/启动槽 B |
| mtd3 | 96 MiB | `system0` | 双系统根 A（UBIFS） |
| mtd4 | 96 MiB | `system1` | 双系统根 B（UBIFS），**当前运行** |
| mtd5 | 24 MiB | `data` | 用户数据（UBIFS，正常启动挂 `/data`） |

### 运行时挂载（实测）

```text
/dev/ubi0_0  on /      type ubifs (rw,noatime)    # 79.2M 容量，已用 32.3M
/dev/ubi1_0  on /data  type ubifs (rw,relatime)   # 14.8M 容量
```

> 注意：正常启动下有 `ubi0`(system1) 与 `ubi1`(data) 两个 UBI 设备；failsafe 下只 attach 了 `ubi0`，`/data` 为空目录。

### 与 S12A（本文原目标）的差异

| 维度 | 本机 S12 1.74.18 | 本文 S12A |
| --- | --- | --- |
| 系统分区 | 96 MiB × 2，**UBIFS** | ~32 MiB × 2，**squashfs** |
| `data` 分区 | **mtd5** | mtd6 |
| 根可写性 | **rw**（可直接改 `/etc`） | 只读根 + `/data` |
| 串口密码 | DSA/PAM，`md5(SN+盐)` 已失效 | 本文按 SN 算密码 |

### 已做改动（含回滚）

| 改动 | 位置 | 备份 | 效果 |
| --- | --- | --- | --- |
| `ttyS0::askfirst:/bin/ash --login` | `/etc/inittab`（写入 system1 的 rw rootfs，**持久**） | `/etc/inittab.bak` | 正常启动后串口直接掉 root shell，不再出现 `S12 login:` |

回滚：

```sh
cp -a /etc/inittab.bak /etc/inittab && sync && reboot
```

### 进入 root shell 的实测流程

1. `screen /dev/tty.usbserial-3120 115200`（先开串口）
2. 音箱上电，从上电起**反复 `f` → `Enter`**
3. 进 failsafe：`root@(none):/#`
4. 正常启动后（已改 `inittab`）：`root@S12:/#`

### 阶段结论

- ✅ **可进系统**，已取得持久 root shell
- ✅ **SSH 可用**（临时进程 + 可选开机自启，见 §16.1 / §16.6）
- ❌ 正常 `S12 login:` 密码路径不可用（1.74.x 走 DSA/PAM）
- ❌ **S12A 寄生端点方案在此机型不成立**（`libxaudio_engine.so` 不存在，见 §16.3）
- 🟡 S12 的替代路线是「接管 pns」，**验证未做**（§16.7），代价是丢原厂 AEC/唤醒
- 🚫 **不要**执行本文 `patch_s12a_rootfs.py`、不要按 S12A 的 mtd 编号/镜像大小写分区、不要封 OTA

---

## 0. 决策门：先看这张表再动手


| 阶段  | 名称                        | 是否写 NAND    | 风险         | 通过标准                                  |
| --- | ------------------------- | ----------- | ---------- | ------------------------------------- |
| A0  | 米家 App 查版本（零成本）           | ❌           | 无          | 记录到 ROM 版本                            |
| A   | 串口只读摸底                    | ❌           | 低          | 版本 + 三个哈希 + 分区全部对上                    |
| B   | 全量备份（4 个分区 + /data + env） | ❌ 只读        | 低          | 4 份镜像 `unsquashfs -s` 通过 + 可启动        |
| C   | 打通 boot0 SSH（**刷机①**）     | ✅ mtd4      | **中高**     | 写回 hash 一致 + 能 SSH                    |
| D   | 打通 boot1 SSH（**刷机②**）     | ✅ mtd5      | **高**（有坏块） | `mtd -f write` 读回 hash 一致 + 能 SSH     |
| E   | 封 OTA + 双系统校验             | ✅ 两套 rootfs | 中          | `verify_owner_maintenance.sh` 两行 PASS |
| F   | 部署寄生端点（耳朵+喇叭）             | ❌ 只写 /data  | 低          | tap 流能出 16k PCM；喇叭能出声                 |
| G   | 接 Mac 的 jarvis            | ❌           | 低          | 语音进 → 设备动 → 语音出                       |
| H   | AEC / 打断 / 延迟三验证          | ❌           | 低          | 见 §10                                 |
| I   | 自启动 + 断电恢复                | ❌ 只写 /data  | 低          | 拔电冷启动后自动恢复                            |


**红线（任一条不满足就停在原地）**

1. **A 阶段哈希对不上 → 全停**。ABI 不匹配的注入会让原生音频链路错乱。
2. **B 阶段没有可用备份 → 不许进 C**。
3. **不写当前正在运行的根分区**。永远"从 A 写 B"。
4. **不写内核分区** `mtd2/boot0`、`mtd3/boot1`。原厂 failsafe 在 boot0 里，项目实机曾因覆盖内核**永久丢掉 failsafe**。
5. **system1（mtd5）有已知坏块**：只能用 `mtd -f write`，禁用 `dd of=/dev/mtdblock5` 和 `nandwrite`（实测读回 hash 不一致）。
6. **串口从头到尾不能拔**。它是唯一救援通道。

---



## 1. 准备



### 1.1 采购清单（≈ ¥30~50）


| 件          | 规格 / 搜索词                                                                     | 说明                  |
| ---------- | ---------------------------------------------------------------------------- | ------------------- |
| USB-TTL 模块 | CH340 或 CP2102，**带 3.3V/5V 均可**                                              | 只用 TX/RX/GND        |
| 端子线        | 搜 `SH1.0 转 2.54 杜邦 3P 端子线 20CM`，选规格 `SH1.0转2.54杜邦端子线-3P`，杜邦端选 **2.54 mm 母头** | 主板是 JST 座，免焊接       |
| 万用表        | 任意                                                                           | 上电前确认 GND / 不要接错电源脚 |


⚠️ **绝对不要**把 USB-TTL 的 `5V/3V3` 接到主板上；也不要只按线色判断 TX/RX/GND。

### 1.2 Mac 侧工具

```bash
brew install squashfs fakeroot          # unsquashfs / mksquashfs / fakeroot
ls /dev/tty.*                          # 接上 USB-TTL 后看设备名
```



### 1.3 待填占位符（接串口后逐项填）


| 项                                     | 文档基线值（用于对比）                                                        | 实测值 | 结论  |
| ------------------------------------- | ------------------------------------------------------------------ | --- | --- |
| 产品型号                                  | MDZ-25-DA / hostname `S12A`                                        |     |     |
| PCB 丝印                                | `DKSND-S12C-ECHO-AB-20180820`                                      |     |     |
| 当前根分区                                 | `mtdblock4`=系统0 / `mtdblock5`=系统1                                  |     |     |
| ROM 版本                                | 系统0=**1.54.8**，系统1=**1.76.54**                                     |     |     |
| `/usr/bin/mipns-xiaomi` SHA256        | `a02071a39f3509d3a90dac638e323039a24de784a907f076a0a91f81cd5fbb9d` |     |     |
| `/usr/lib/libxaudio_engine.so` SHA256 | `79f1a33d9683cd6940c8d23e3dd4e992f1958fe220c0dfff8d230f9f8a1b5e73` |     |     |
| `/etc/init.d/pns` SHA256              | `d9544da4c47bebce556eb656ff5679a6eb90b49255d51f5f740c74706e25a1d7` |     |     |
| 音箱 LAN IP                             | 示例 `192.168.8.152`                                                 |     |     |
| Mac LAN IP                            | 示例 `192.168.8.150`                                                 |     |     |
| 串口设备                                  | 示例 `/dev/tty.usbserial-3120`                                       |     |     |


---



## 2. 阶段 A0：米家 App 查版本（今天就能做，零成本）

1. 米家 App → 该音箱 → 右上角 ⚙️ → 关于 / 版本。
2. 记录版本号，填进 §1.3。

- 若是 **1.54.8 或 1.76.54**（双系统各一个）→ 继续。
- 若是更新的版本 → ⚠️ 先别买线：安装器按"已审核 ROM"白名单，新 ROM 必须重新审核内核/驱动/启动路径/分区容量/升级入口，**不能只改版本号绕过**。

---



## 3. 阶段 A：串口只读摸底（不写任何东西）



### A1 接线与进串口

```bash
# 接线：模块 TXD → 音箱 RXD，模块 RXD → 音箱 TXD，GND → GND
screen /dev/tty.usbserial-XXXX 115200      # 退出：Ctrl+A → K → Y
```

定位技巧：主板上有多处 JST，**上电时看哪一组持续吐启动日志**就是它；先万用表确认 GND。
注意：仓库参考照是绿色板，你这块是黑色阻焊，**插座位置/朝向可能不同，不要照图硬插**。

### A2 只读检查项

进 U-Boot（上电时按任意键，提示符 `s12#`）：

```text
s12# printenv boot_part
s12# printenv boot_failcnt boot_failed storeboot initargs
s12# printenv                      # 全部记下来存档
```

等系统起来后，通过串口 shell（或已有的任何 root shell）执行：

```sh
mount | grep ' on / '               # 判断当前是 mtdblock4 还是 mtdblock5
cat /proc/mtd
cat /proc/cmdline
uname -a                            # 期望 Linux 4.9.61 aarch64
sha256sum /usr/bin/mipns-xiaomi /usr/lib/libxaudio_engine.so /etc/init.d/pns
ls -l /data/ /data/init.sh 2>/dev/null
```



### A3 验收 / 失败处理

- ✅ 三个 SHA256 与 §1.3 基线**完全一致**，`uname` 是 4.9.61 aarch64 → 进 B。
- ❌ 任一不符 → **停**。可选动作：把实测哈希记进 §1.3，然后判断是否需要重新评估（不去猜 ABI）。

---



## 4. 阶段 B：全量备份（唯一的后悔药）

顺序：先备份，再抄 `/data`，最后抄 U-Boot env。

```bash
mkdir -p ~/backup/s12a-$(date +%Y%m%d) && cd ~/backup/s12a-$(date +%Y%m%d)

# 1) 四个分区（可在串口 shell 里 dd 到 U 盘/网络，或 SSH 起来后照下方命令）
for p in 2 3 4 5; do
  dd if=/dev/mtdblock$p of=/tmp/mtd${p}.img bs=64k || true
  gzip -c /tmp/mtd${p}.img > mtd${p}.img.gz
done

# 2) 完整 /data（含 dropbear 公钥、配置、dirac 配置 —— 可能含密钥，不进 Git）
tar czf data-full.tar.gz -C / data

# 3) 元信息
{ date; uname -a; cat /proc/mtd; mount; cat /proc/cmdline; \
  sha256sum /usr/bin/mipns-xiaomi /usr/lib/libxaudio_engine.so /etc/init.d/pns; } > env.txt 2>&1
```



### B1 校验（不合格不许进 C）

```bash
gzip -dc mtd4.img.gz > mtd4.img && unsquashfs -s mtd4.img   # 期望 Found a valid SQUASHFS 4:0, xz, 131072
gzip -dc mtd5.img.gz > mtd5.img && unsquashfs -s mtd5.img
ls -l mtd*.img                                              # 每个应为 33554432 字节（32 MiB）
shasum -a 256 mtd*.img data-full.tar.gz >> SHA256SUMS
```

> ⚠️ 读 `mtdblock5` 时出现 `Input/output error` **不一定**代表备份废了——system1 尾部/坏块是已知现象。判定标准是"文件大小合理 + `unsquashfs -s` 识别成功 + 完整解包无错"。
> 项目文档明确：不能只凭超级块识别成功就认为备份有效，**必须完整解包一次并检查错误**。

```bash
rm -rf /tmp/v5 && unsquashfs -d /tmp/v5 mtd5.img 2>&1 | tee mtd5-unpack.log
grep -i 'error' mtd5-unpack.log || echo "解包无错误 ✅"
```

---



## 5. 阶段 C：打通 boot0 SSH（**刷机①：改 rootfs**）

> 这也是"刷机"的真正含义：给只读 squashfs 注入一个开机 hook，然后写回分区。之后所有改动都放 `/data`，不再反复写 NAND。



### C1 先确认恢复条件（重要）

原始 failsafe 靠 boot0 里的 kernel + initramfs 提供，"切回 boot0 就能救"**不成立**——项目实机就因为覆盖内核永久丢了 failsafe。所以：

- 只有在 A2 的启动日志里**亲眼看到** `Press the [f] key and hit [enter]` 这行，才算 failsafe 可用。
- 看不到 → 你的唯一后路就是"备份镜像 + 串口 + U-Boot"，**没有任何交互式救援 shell**，风险等级上调一档。



### C2 进 failsafe 并临时联网（当前系统不能 SSH 时）

```sh
mknod /dev/ubi_ctrl c 10 58 2>/dev/null
ubiattach -m 6 -d 0
mknod /dev/ubi0_0 c 242 1
mkdir -p /tmp/data && mount -t ubifs ubi0:data /tmp/data

insmod mlan
insmod sd8xxx "drv_mode=3 cfg80211_wext=0xf cal_data_cfg=mrvl/WlanCalData_ext.conf \
fw_name=mrvl/sdsd8977_combo_v2.bin sta_name=wlan mac_addr=<音箱WiFi MAC> reg_alpha2=CN drvdbg=0x80007 ps_mode=2 auto_ds=2"
sleep 5

ifconfig wlan0 up
wpa_supplicant -B -Dnl80211 -i wlan0 -c /tmp/data/wifi/wpa_supplicant.conf   # SSID/PSK 按你家改
sleep 8
ifconfig wlan0 <音箱IP> netmask 255.255.255.0 up
route add default gw <网关>
busybox telnetd -l /bin/sh -p 2323 &
```

Mac 侧 `nc <音箱IP> 2323` 进去；此时把 `/tmp/mtd4.img.gz` 传出去（HTTP / nc 任选）。

### C3 在 Mac 上打补丁（推荐走工具，不手搓）

```bash
git clone https://github.com/open-speaker-llm/xiaomi_ai_llm.git ~/src/xiaomi_ai_llm

# 推荐：用审核过的构建器（只在本机做镜像，不碰设备）
python3 ~/src/xiaomi_ai_llm/tools/speaker-maintenance/patch_s12a_rootfs.py \
  --input  /绝对路径/mtd4.img \
  --input-sha256 <§1.3 实测 sha256> \
  --rom 1.54.8 \
  --output-dir /绝对路径/新的构建目录
# 产物：manifest.json（含 flashed:false）+ 32 MiB 的补丁镜像
```

手搓等价流程（想理解原理时看，不要和工具混用）：

```bash
unsquashfs -d root mtd4.img
cp ~/src/xiaomi_ai_llm/device/init.d/sshen root/etc/init.d/sshen && chmod 755 root/etc/init.d/sshen
ln -sf ../init.d/sshen root/etc/rc.d/S45sshen

cat > pseudo.txt <<'EOF'
/dev/console c 0600 0 0 5 1
/dev/null c 0666 0 0 1 3
/dev/ptmx c 0666 0 0 5 2
EOF

fakeroot mksquashfs root mtd4_ssh.img -comp xz -b 131072 -no-xattrs -all-root \
  -pf pseudo.txt -noappend
dd if=mtd4_ssh.img of=mtd4_ssh_padded.img bs=33554432 conv=sync count=1
gzip -c mtd4_ssh_padded.img > mtd4_ssh_padded.img.gz
unsquashfs -s mtd4_ssh_padded.img && shasum -a 256 mtd4_ssh_padded.img
```

> 补丁只加 4 样东西：SSH hook（`S45sshen`，`START=45`，echo 1 > `/tmp/ssh_en`，bind mount 公钥）、关掉 dropbear 密码认证、注释 `/etc/crontabs/root` 里唯一的 `/bin/ota slient`、把 `/bin/ota` 和 `/bin/flash.sh` 换成 `exit 126` 的拒绝脚本（原脚本另存 `/usr/lib/owner-maintenance/*.original`，0600）。



### C4 写回 mtd4

```sh
# 在设备 root shell 里
gzip -dc /tmp/mtd4_ssh_padded.img.gz > /tmp/mtd4_ssh.img
dd if=/tmp/mtd4_ssh.img of=/dev/mtdblock4 bs=64k
sync && reboot
```

写之前再一次：`ls -l /tmp/mtd4_ssh.img`（必须是 33554432 字节）、`cat /proc/mtd`（确认 mtd4=system0）。

### C5 验收

```bash
ssh-keygen -R <音箱IP>
ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedKeyTypes=+ssh-rsa root@<音箱IP>
# 进去后
mount | grep ' on / '        # 应为 /dev/mtdblock4
cat /tmp/ssh_en              # 1
ls -l /etc/init.d/sshen /etc/rc.d/S45sshen
```

失败 → 串口进 U-Boot 检查 `boot_part`，用备份镜像重写 mtd4；**不要在 boot1 状态下原地重写 boot0**。

---



## 6. 阶段 D：打通 boot1 SSH（**刷机②：写 mtd5，注意坏块**）

```bash
# 1) 从当前 boot0 系统读出 system1（若 §4 已有 mtd5.img，直接用它）
ssh root@<音箱IP> 'dd if=/dev/mtdblock5 bs=64k 2>/tmp/dd5.err' | gzip -c > mtd5_now.img.gz
gzip -dc mtd5_now.img.gz > mtd5_now.img
unsquashfs -s mtd5_now.img          # 必须能识别出有效 squashfs 才能往下走

# 2) 同 C3 打补丁，但 --rom 1.76.54
python3 tools/speaker-maintenance/patch_s12a_rootfs.py \
  --input mtd5.img --input-sha256 <实测> --rom 1.76.54 --output-dir /绝对路径/out5

# 3) 上传 + 校验远端 hash
scp -O mtd5_ssh_padded.img.gz root@<音箱IP>:/tmp/
ssh root@<音箱IP> 'sha256sum /tmp/mtd5_ssh_padded.img.gz; df -h /tmp'
#    远端 hash 必须等于 Mac 上的

# 4) 必须用 mtd 写（禁 dd / nandwrite）
ssh root@<音箱IP> 'gzip -dc /tmp/mtd5_ssh_padded.img.gz | mtd -f write - system1; sync'
#    日志出现 "Skipping bad block at 0x00060000" 属已知现象

# 5) 读回校验（必须一致）
ssh root@<音箱IP> 'dd if=/dev/mtdblock5 bs=64k count=512 2>/dev/null | sha256sum'

# 6) 切 boot1 验证
screen /dev/tty.usbserial-XXXX 115200      # 重置时进 U-Boot
#   s12# setenv boot_part boot1
#   s12# saveenv
#   s12# reset
ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedKeyTypes=+ssh-rsa root@<音箱IP>

# 7) 失败回退
#   s12# setenv boot_part boot0
#   s12# saveenv
#   s12# reset
```

**boot1 专属红线**：不要把 boot0 的原生音频库拷过来覆盖 boot1（会破坏识别）。配置必须保持：

```sh
AUDIO_CAPTURE_SETUP=auto
NATIVE_RESULT_SOURCE=auto
WAKE_ON_THINK_SYSTEM1=1
NATIVE_AIVS_LAB_RESULT_SYSTEM1=1
```

❌ **绝不要**在 boot1 上把 `AUDIO_CAPTURE_SETUP` 强制设成 `1`：会导致原生 `recorder` 崩溃，表现为**能唤醒但后续 ASR/NLP、开关灯、天气全不响应**。

### D1 双系统验收

两套系统分别单独验证：SSH + 原生唤醒报时 + 家电控制 + LLM 首轮。**只打通一边不算完成**——失败切换机制可能把设备踢到另一边。

---



## 7. 阶段 E：封 OTA + 受控升级

```bash
ssh root@<音箱IP> 'sh -s /dev/mtdblock4' < ~/src/xiaomi_ai_llm/tools/speaker-maintenance/verify_owner_maintenance.sh
# 期望输出两行 PASS；切到 boot1 后把参数换成 /dev/mtdblock5 再跑一次
```

检查点：

- `S45sshen` 存在且挂载 `/data/dropbear/authorized_keys`
- dropbear 密码认证已关（禁止密码登录）
- `/etc/crontabs/root` 里 `/bin/ota slient` 已注释
- `/bin/ota`、`/bin/flash.sh` 返回 126
- **保留原来的失败切换逻辑**——封 OTA ≠ 封换槽

> 想升级时走"人工受控升级"：拿官方固件 → 本机提取 → 打补丁 → 只写备用分区 → 读回 hash → 启动验证 → 保留旧系统直到新系统验完。

---



## 8. 阶段 F：部署寄生端点（耳朵 + 喇叭）



### F1 原则（做错就全废）


| 原则                      | 原因                                           |
| ----------------------- | -------------------------------------------- |
| 麦克风**不能**抢 ALSA         | `/dev/snd/pcmC0D2c` 被 `mipns` 独占；抢了就丢 AEC/前端 |
| 播放**必须**走固件 ALSA/DSP 路由 | 绕开它 → AEC 参考平面拿不到你的播放声 → 自激 + 唤醒失效           |
| 只动 `/data`，每次开机重建       | rootfs 只读；bind mount 不持久                     |
| 校验固件哈希再注入               | ABI 绑死，不匹配会搞乱音频链路                            |




### F2 编译（在 Mac / Linux 上交叉编）

```bash
# ring 后端 + 采集器
sh device/pcm_tap/build.sh /tmp/s12a-build        # 用 Zig 0.16.0

# Dirac 播放包装
sh device/dirac/build.sh /tmp/s12a-dirac-build
```

产物与目标（**注意架构不同，不能同一编译目标**）：


| 产物                      | 目标                   | 说明                            |
| ----------------------- | -------------------- | ----------------------------- |
| `xaudio_pcm_tap.so`     | **ARM32** glibc 2.25 | `LD_PRELOAD` 进 `mipns-xiaomi` |
| `capture_pcm`           | **ARM64** musl 静态    | 从 ring 读 PCM → WAV/流          |
| `native_first_dirac.so` | ARM                  | 只给 aplay 进程用                  |




### F3 部署到 /data

```sh
# 装到设备（示例，具体以各组件 README 的安装器为准）
#   /data/xaudio_pcm_tap.so  /data/capture_pcm  /data/native_pcm_tap.sh
#   /data/native_first_dirac.so  /data/dirac_aplay.sh
#   /data/native_first.env（配置）
#   /data/init.sh（开机入口）
# 所有可执行文件 chmod 700，属主 root

sh /data/native_pcm_tap.sh start      # 注入（内部先校验 3 个 SHA256，不符直接拒绝）
sh /data/native_pcm_tap.sh status
```



### F4 耳朵：拿到 16 kHz PCM

**路径**：`LD_PRELOAD` → hook `int xaudio_wrapper_dnn(void*, void*, int, void*, int*)` → 原函数照常调用、返回值与输出都不改 → 只在返回后**复制**其输出。


| 项    | 实测值                                                           |
| ---- | ------------------------------------------------------------- |
| 每次调用 | 2 段 × 160 个 S16 样本，约 100 次/秒                                  |
| 取用   | 第一段 → 16 kHz 单声道 S16LE、10 ms 帧                                |
| 落点   | `/tmp/native_followup_pcm.ring`（0600，82,976 字节 ≈ 2.56 s 环形缓冲） |
| 防撕裂  | 序号发布 + 前后复查；生产者更换/停滞 >2 s/消费者落后/帧校验失败 → 退出                    |
| 流率   | 32 kB/s ≈ 256 kbps（局域网无压力）                                    |


```sh
sh /data/native_pcm_tap.sh check        # 只验证新帧在流动，不出录音
/data/capture_pcm /tmp/probe.wav 8      # 录 8 秒做体检
```

**验收**：`ffprobe` 为 `16000 Hz, 1 ch, s16le`；播放整段人声清晰；**静音段不该有周期性咔哒**（有 = 撕裂/落后）。

### F5 喇叭：能播任意 PCM

```sh
sh /data/dirac_aplay.sh /tmp/probe_out.wav
# 日志 /tmp/native_first_dirac.log 必须出现：
#   [DIRAC] initialize rc=0 config=/data/etc/diracmobile.config
#   ... Prepared with conf: 'speaker'
# 且没有 Dirac_create / Dirac_prepareToPlay / Dirac_setVolume / Dirac_processBlock 错误
```

> 为什么必须走它：原生 mediaplayer 在进程内调 `Dirac_initialize(...)`，独立 aplay 不会，于是去找不存在的 `/usr/lib/diracmobile.config`，音效实例创建失败 → 音质异常/没声。`LD_PRELOAD` **只给这个 aplay 进程**，不全局预加载。

**验收**：日常音量下语音清晰、首尾完整、无爆音；连续播 1 分钟不丢句；播报期间喊"小爱同学"仍能开灯且播报不断（这条同时验证 AEC）。

---



## 9. 阶段 G：接进 home-jarvis（只换 audio 层）

```
┌──────────── MDZ-25-DA（原生固件不动）────────────┐
│ mipns-xiaomi ──LD_PRELOAD──> xaudio_pcm_tap.so    │
│        └─> /tmp/*.ring  16k/mono/S16LE/10ms       │
│ 常驻 reader (C) ──WebSocket──> Mac                │  ← 耳朵
│ /data/dirac_aplay.sh <──WebSocket── Mac           │  ← 喇叭（走固件 ALSA，AEC 参考成立）
└───────────────────────────────────────────────────┘

Mac: ws server ─> jarvis/audio.py 输入侧（复用 segmenter / KWS / barge-in）
      jarvis TTS ─> ws 下发 PCM ─> 音箱 aplay
```

要动的地方（**只这一层**，状态机 / 工具层 / LLM / prompt 一行不改）：


| 文件                    | 改动                                                                   |
| --------------------- | -------------------------------------------------------------------- |
| `jarvis/audio.py`     | 新增输入源 `RingSocket`（16k mono，等价于现有录音流）；输出走 `SpeakerSink` 而非 PortAudio |
| `config/aliases.yaml` | 不动                                                                   |
| `.env`                | 新增 `AUDIO_SOURCE=speaker_endpoint`、`SPEAKER_WS=ws://<音箱IP>:<port>`   |
| 复用点                   | `TARGET_RATE=16000` 已经对齐；`silero-vad` / KWS / `SpeechRouter` 全部原样    |


**和 P1（ESP32-S3）的关系**：两者抽象完全一致（16k mono in / PCM out），可以互为备份——音箱给"远场 + AEC + 好喇叭"，ESP32 给"全可控 + 真打断"。客厅固定端用音箱，实验端用 ESP32。

---



## 10. 阶段 H：三个验证实验（决定后续设计）



### H1：AEC 到底在不在 tap 流里？

原理：固件 `xaudio_aec_thread` 吃 **7 个平面 × 128 个 S16**——前 6 个是麦克风，**第 7 个是参考音频**；**参考峰值 < 10 时直接复制麦克风样本、跳过 AEC 运算**。而参考通道默认是**关**的（Loopback Disable），必须在原生采集**启动前** Enable。

```sh
# 1) 确认参考通道状态（组件管理脚本的 status 应报告 Loopback Enable）
# 2) 用 dirac_aplay 播一段长语音；同时录 tap 流
/data/capture_pcm /tmp/during_playback.wav 8
# 3) 听/看：播报期间人声是否还能干净分离
```


| 结果         | 含义        | 后续设计                                               |
| ---------- | --------- | -------------------------------------------------- |
| 人声干净、回声被压掉 | ✅ AEC 生效  | 可做真 barge-in；你的 VAD 不会被自己播报触发                      |
| 人声被自己播报淹没  | ❌ AEC 未生效 | 先查 Loopback 是否在**采集启动前** Enable；仍不行 → 退回半双工（说话时静音） |




### H2：能做真 barge-in 吗？

> 社区客户端的"不能打断"是**它的客户端限制**（`capture_pcm` 固定 8 s 窗口、播完才收），**不是硬件限制**。ring 是连续流动的。

自测方法：写一个常驻 reader 持续读 ring → 检测到你说话（能量/VAD）→ `kill` 播放进程。能停住且不啸叫 = barge-in 成立。

### H3：延迟够不够 0.7~1.5 s 首字

```sh
# 打时间戳：shim 写入 ring 的时刻 vs Mac 收到时刻，统计 p50/p95 抖动
# 目标：10 ms 帧经 LAN 的 p95 < 100 ms，否则首字目标要打折
```

---



## 11. 阶段 I：自启动与断电恢复

```sh
# 开机入口：/data/init.sh（rc.local 里 [ -f /data/init.sh ] && sh /data/init.sh &）
# 组件必须每次开机重建 bind mount / LD_PRELOAD，不能把一次手动挂载当成已持久安装
```

验收：

1. `reboot` 后：SSH 自动可用、客户端自动起、tap 流自动流动、喇叭能播。
2. **拔电源冷启动**后再验一次（用户断电是最真实场景）。
3. 检查 `/tmp/native_first_autostart.log`、`/tmp/native_first_client.log`、`/tmp/native_first_dirac.log`。

---



## 12. 回退与救砖


| 症状          | 处理                                                                        |
| ----------- | ------------------------------------------------------------------------- |
| 注入后没声音/唤醒失效 | 组件 stop（`native_pcm_tap.sh stop`）+ 配置 `SYSTEM1_FOLLOWUP_ENABLED=0`，重启原生服务 |
| 装完不进系统      | 串口进 U-Boot → `setenv boot_part boot0` → `saveenv` → `reset`               |
| 两套都进不去      | 串口 + U-Boot（**前提：failsafe 或 U-Boot 还在**）→ 用 §4 备份镜像重写备用分区                 |
| 想彻底回到原厂     | 用 §4 的 `mtd4.img` / `mtd5.img` 原样写回备用分区，读回 hash 校验，再切槽                    |


组件自带 `restore.sh`（安装器输出 `BACKUP=/data/native-pcm-backup-<时间>`），优先用它，别手搓。

---



## 13. 风险清单（写全）

1. **变砖**：写错 `mtd2/mtd3`（内核）→ 原厂 failsafe 可能**永久丢失**，项目实机踩过。恢复只能靠 U-Boot + 备份镜像。
2. **system1 坏块**：`dd`/`nandwrite` 实测读回不一致，只有 `mtd -f write` 有效。
3. **固件绑死 ABI**：三个 SHA256 任一变化 → 不注入。装完必须封 OTA，否则一次自动升级就把 tap 打回原形。
4. **封 OTA ≠ 封换槽**：失败切换仍会换 boot 分区，两套系统都要各自验证 SSH。
5. **AEC 依赖配置**：参考通道必须在原生采集启动前 Enable；播放绕开固件 ALSA → AEC 失效 + 自激。
6. **拆机/保修**：官方不认，后果自负。
7. **法律**：仅限自有设备研究；仓库与本文都不应携带/分发任何小米固件、分区 dump、私有二进制。
8. **不要期望"硬件 AEC"**：这台机器的 AEC 是闭源用户态库在 CPU 上跑的，不是独立硬件模块。
9. **全天稳定性未验证**：上游只验证到"整机重启 + 断电冷启动恢复"，长时间运行、更多噪声/距离条件仍是待办。
10. **播放中打断**：机制上可行，但社区实现里**没有**，要你自己写；写不出来就退半双工。

---



## 14. 完成定义（DoD）

- [ ] §1.3 占位符全部填写，三个哈希与基线一致
- [ ] 4 份分区镜像 + `/data` + U-Boot env 备份齐备，全部校验通过
- [ ] 两套系统都能 SSH、都能单独完成"唤醒 → 家电控制 → LLM 首轮"
- [ ] `verify_owner_maintenance.sh` 两套各两行 PASS
- [ ] tap 流 16 kHz/单声道/S16LE 稳定，静音段无咔哒
- [ ] `dirac_aplay` 日志 `initialize rc=0`，连续播报 1 分钟无异常
- [ ] H1 实验结论明确（AEC 生效 / 不生效 → 对应设计）
- [ ] Mac 上 `jarvis` 只用 `AUDIO_SOURCE=speaker_endpoint` 就能完成"语音进 → 设备动 → 语音出"
- [ ] 断电冷启动后自动恢复

---



## 15. 来源

- 硬件与风险：[docs/reference/hardware.md](https://github.com/open-speaker-llm/xiaomi_ai_llm/blob/main/docs/reference/hardware.md)
- 启动链路与双系统：[docs/concepts/boot-and-partitions.md](https://github.com/open-speaker-llm/xiaomi_ai_llm/blob/main/docs/concepts/boot-and-partitions.md)
- 从零接入路线：[docs/getting-started/bringup.md](https://github.com/open-speaker-llm/xiaomi_ai_llm/blob/main/docs/getting-started/bringup.md)
- boot0 SSH 手册（写入 mtd4）：[docs/runbooks/boot0-ssh.md](https://github.com/open-speaker-llm/xiaomi_ai_llm/blob/main/docs/runbooks/boot0-ssh.md)
- boot1 SSH 手册（写入 mtd5，坏块）：[docs/runbooks/boot1-ssh.md](https://github.com/open-speaker-llm/xiaomi_ai_llm/blob/main/docs/runbooks/boot1-ssh.md)
- 双系统 SSH 与受控升级：[docs/runbooks/owner-maintenance.md](https://github.com/open-speaker-llm/xiaomi_ai_llm/blob/main/docs/runbooks/owner-maintenance.md)
- PCM 采集（hook 点 / ABI / ring）：[device/pcm_tap/README.md](https://github.com/open-speaker-llm/xiaomi_ai_llm/blob/main/device/pcm_tap/README.md)
- AEC 参考通道（7 平面 × 128 样本）：[docs/history/2026-09-09-parallel-wake-reference.md](https://github.com/open-speaker-llm/xiaomi_ai_llm/blob/main/docs/history/2026-09-09-parallel-wake-reference.md)
- Dirac 播放初始化：[device/dirac/README.md](https://github.com/open-speaker-llm/xiaomi_ai_llm/blob/main/device/dirac/README.md)
- 能力与边界：[docs/status.md](https://github.com/open-speaker-llm/xiaomi_ai_llm/blob/main/docs/status.md)

---

## 16. 附录：S12 / 1.74.18 专线（本文原目标之外的机器）

> 适用：丝印 `DKSND-S12-ECHO-AB-20170210`、ROM `1.74.18`、`meson/gxl`、UBIFS 双系统。
> **本节与正文 §4~§9 不通用**；正文是 S12A + squashfs 路径。

### 16.1 临时开 SSH（PAM + dropbear）

社区在 1.74.15 / 1.74.18 上验证过；做法是把 PAM 认证放宽，再手起 dropbear。

**前提**：串口已有 root shell（见 §0.0）。

```sh
# 1) 备份
cp -a /etc/pam.d/common-auth /etc/pam.d/common-auth.bak

# 2) 放宽认证（把 common-auth 改成三行）
cat > /etc/pam.d/common-auth <<'EOF'
auth [success=1 default=ignore] pam_unix.so nullok_secure
auth requisite pam_deny.so
auth required pam_permit.so
EOF
sync

# 3) 起 dropbear（1.74.18 自带 host key 可能已存在）
ls /etc/dropbear/
[ -f /etc/dropbear/dropbear_rsa_host_key ] || dropbearkey -t rsa -f /etc/dropbear/dropbear_rsa_host_key
mkdir -p /var/run
dropbear -r /etc/dropbear/dropbear_rsa_host_key -p 22

# 4) 确认在听
netstat -lntp 2>/dev/null | grep 22
ifconfig
```

Mac 侧：

```bash
ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedKeyTypes=+ssh-rsa root@<音箱IP>
```

**回滚**：

```sh
cp -a /etc/pam.d/common-auth.bak /etc/pam.d/common-auth
killall dropbear
sync
```

⚠️ 注意：
- PAM 放宽后 **任何能连到 22 端口的人都能进 root**——只在可信内网做，做完建议改回或用密钥。
- `dropbear` 是手动起的进程，**重启不保留**；要持久需写 `/etc/init.d` + `/etc/rc.d`（另做）。
- 系统里可能自带 `chen@beijing` 的 authorized_keys，确认后删掉。

### 16.2 音频链路侦察（只读，先摸清再谈注入）

```sh
# 组件与库
ls -l /usr/bin/mipns-xiaomi /usr/lib/libxaudio_engine.so /etc/init.d/pns 2>/dev/null
sha256sum /usr/bin/mipns-xiaomi /usr/lib/libxaudio_engine.so /etc/init.d/pns 2>/dev/null

# 相关二进制/库全景
ls /usr/bin | grep -iE 'pns|xaudio|dirac|aplay|arec|pcm|mico'
ls /usr/lib | grep -iE 'xaudio|dirac|asound|pcm'

# ALSA 设备
cat /proc/asound/cards
cat /proc/asound/pcm
ls -l /dev/snd/

# 正在跑的原生音频进程
ps w | grep -iE 'pns|xaudio|dirac|aplay|tinycap|mico' | grep -v grep

# 打开的文件/共享内存（找采集与播放的落点）
ls -l /dev/shm/
ls /tmp | head -50
```

**要记下来的三件事**（填回 §0.0 表或另开一节）：

1. `mipns-xiaomi` / `libxaudio_engine.so` / `/etc/init.d/pns` 的 **SHA256**
2. ALSA card/pcm 命名，以及采集与播放分别走哪个设备
3. `pns` 服务的启动方式（`/etc/init.d/pns` 里 exec 了谁）

**红线**：S12 的这三个哈希与 S12A 基线**必然不同**，`libxaudio_engine.so` 的 ABI（7 平面 × 128 S16、hook 点、ring 布局）也未验证；在实测对比前**不得**套用 `device/pcm_tap` 与 `device/dirac` 的实现。

### 16.3 实测结论（2026-11-01，S12 / 1.74.18）

#### SSH 状态：可用（临时）

```text
dropbear 已装 PAM 支持（libpam.so.0）
/etc/pam.d/common-auth = [success=1 default=ignore] pam_unix.so nullok_secure
                          requisite pam_deny.so
                          required pam_permit.so
root@192.168.1.3:22  LISTEN
```

⚠️ 该配置**并非免密**：`pam_unix` 通过后 `success=1` 跳一行，失败则落到 `pam_deny`。实际仍需正确密码或公钥。

- host key 原始文件是 0 字节（导致 `Bad buf_getptr`），已用 `dropbearkey -t rsa -s 2048` 重建（旧文件存为 `dropbear_rsa_host_key.bad`）
- 该 dropbear **只支持 rsa key 类型**（`-t ecdsa` 报 `Unknown key type`）
- dropbear 是手动前台/后台进程，**重启不保留**
- `/etc/dropbear/authorized_keys` 里有 738 字节的**出厂遗留公钥**（很可能就是 `chen@beijing`），落地前应清理

#### 音频链路：与 S12A 完全不同，`libxaudio_engine.so` 不存在

| 探测项 | S12 / 1.74.18 实测 | S12A 文档假设 |
| --- | --- | --- |
| `/usr/lib/libxaudio_engine.so` | ❌ **不存在** | ✅ 核心注入点 |
| `/usr/bin/mipns-xiaomi` | ✅ 339,080 B | 同名 |
| `/etc/init.d/pns` | ✅ 16,848 B | 同名 |
| 音频库 | `libDiracAPI_SHARED.so` + ALSA | `libxaudio_engine.so` |
| ALSA card | `0 [AMLM8AUDIO] AML-M8AUDIO`（Amlogic 内置） | — |
| PCM 节点 | `00-00 I2S.18 T9015-audio-hifi-0`（playback 1 / capture 1）<br>`00-01 SPDIF.19 dit-hifi-1`<br>`00-02 PCM.20 pcm2bt-pcm-2` | — |
| `/dev/snd/` | `pcmC0D0c` `pcmC0D0p` `pcmC0D1c/p` `pcmC0D2c/p` | — |
| `/dev/shm` | 空（无共享内存 ring） | ring 缓冲 |
| 语音栈 | `mipns-xiaomi -c /usr/share/mipns/ -r opus32 -l`（**opus 编解码**） | 16k PCM tap |

正在运行的关键进程：

```text
1142 /usr/bin/pns_ubus_helper
1204 /usr/bin/mipns-xiaomi -c /usr/share/mipns/ -r opus32 -l
1590 /usr/bin/mediaplayer
1609 /usr/bin/mico-helper
1924 /usr/bin/mico_ai_crontab
2383 /usr/bin/mico_aivs_lab
```

#### `pns` 启动链要点（`/etc/init.d/pns`，`START=55`，procd 托管）

- 供应商由 `micocfg_speech_vendor` 决定；S12 走 `_start_mipns_xiaomi` → `mipns-xiaomi -c /usr/share/mipns/ -r opus32 -l`
- 脚本里**有 `S12` 分支**（与 `L09A/LX06/...` 并列），说明 S12 是官方支持型号，但走 **`/usr/share/mipns/`** 而非 S12A 的 `xaudio_engine.conf`
- `S12A` 专属逻辑（`pdm dclk` 调速、`soundai` 6 麦模型）**不适用于 S12**
- 麦克风静音控制走 `ubus call pnshelper event_notify` + `mic_on/mic_off`，另有 `show_led/shut_led`

#### 结论

1. ✅ **SSH 可用**（临时，重跑 `dropbear -p 22` 即可恢复；勿依赖）
2. ❌ **S12A 的 `libxaudio_engine.so` + `pcm_tap` + `dirac` 方案在 S12 上不可用**——目标库根本不存在，注入点不成立
3. 🟡 S12 的语音链路是 **opus32 编码 + mipns 私有协议 + Dirac 播放**，要接 home-jarvis 需重新设计端点（可能要 hook `mipns-xiaomi` 或走 ALSA/Dirac 层）
4. 🚫 不要按正文 §5~§9 动 NAND；S12 双系统是 **UBIFS + ubi0/ubi1**，与 S12A 的 squashfs + mtd 偏移完全不同

### 16.4 待办（2026-11-02 更新）

已完成的：

- [x] 开 SSH 并验证能进（临时进程，`dropbear -p 22`）
- [x] 记录 `mipns-xiaomi` / `/etc/init.d/pns` 路径与大小
- [x] 确认 ALSA 采集/播放节点与 `pns` 启动链
- [x] 确认 `mipns-xiaomi` 的音频 IO 落点（`/proc/<pid>/fd` → `pcmC0D0c` / `pcmC0D0p`）
- [x] 确认 `libxaudio_engine.so` **不存在**（`/proc/<pid>/maps` 无此库）
- [x] 确认无 `snd-aloop` 模块
- [x] 确认 `xaudio_engine.conf` 只是唤醒/VAD 模型配置
- [x] 拿到 `mipns-xiaomi` MD5：`6cd03f9c1a2f517677898015e49e58b7`

未完成 / 阻塞：

- [ ] **接管 pns 的 arecord/aplay 验证**（§16.7 推荐路线）← **当前卡点**
- [ ] `sha256sum` 实际值（设备无 `sha256sum`，只有 md5sum；或从 Mac 侧算）
- [ ] 判断双系统另一槽（system0 / mtd3）版本
- [ ] 清理 `/etc/dropbear/authorized_keys` 出厂公钥
- [ ] dropbear 持久化（§16.6，需 reboot，建议在音频验证后做）
- [ ] 决定：S12 自研端点 or 换一台 S12A 走正文

### 16.4.1 已知环境坑（避免重复踩）

| 坑 | 现象 | 解法 |
| --- | --- | --- |
| SSH 会话 PATH 缺失 | `sha256sum: not found`、`head: not found` | 命令用绝对路径，或 `export PATH=/usr/sbin:/usr/bin:/sbin:/bin` |
| 设备无 sha256sum | 只有 `md5sum` | 用 md5，或在 Mac 侧 `scp` 下来算 |
| `/tmp` 不可写日志 | `-ash: /tmp/xxx.log: Permission denied` | 日志写 `/data/` |
| dropbear host key 空文件 | `Early exit: Bad buf_getptr` | `dropbearkey -t rsa -s 2048 -f ...` 重建 |
| dropbear 不支持 ecdsa | `Unknown key type 'ecdsa'` | 只用 rsa |
| 串口拔插后设备改名 | `-3110` ↔ `-3120` | 先 `ls /dev/tty.*` 再 `screen` |
| TTL 接线导致不启动 | 无启动声、无灯 | 先拔 TTL 验证供电，再逐根接回（勿接 5V/3V3） |

### 16.6 dropbear 开机自启（持久化）

目标：断电重启后 SSH 自动可用，不必再手起。

**前提**：已在串口/SSH 拿到 root shell，且 `/etc/dropbear/dropbear_rsa_host_key` 已重建（见 §16.1）。

**做法**：写一个最小 procd init 脚本。S12 用的是 OpenWrt Chaos Calmer + procd，`/etc/init.d/pns` 也是这个模式。

```sh
cat > /etc/init.d/dropbear <<'EOF'
#!/bin/sh /etc/rc.common
# 本机(S12)专用：开机拉起 dropbear SSH
START=95
STOP=10

USE_PROCD=1

start_service() {
    local key=/etc/dropbear/dropbear_rsa_host_key
    [ -f "$key" ] || {
        logger -t dropbear-init "host key missing: $key"
        return 1
    }
    mkdir -p /var/run /etc/dropbear
    procd_open_instance
    procd_set_param command /usr/sbin/dropbear -F -E -r "$key" -p 22
    procd_set_param respawn 3600 5 5
    procd_set_param stdout 1
    procd_set_param stderr 1
    procd_close_instance
}

service_triggers() {
    procd_add_reload_trigger "dropbear"
}
EOF

chmod 755 /etc/init.d/dropbear
```

先确认 dropbear 的真实路径（不同版本可能不在 `/usr/sbin`）：

```sh
which dropbear
ls -l /usr/sbin/dropbear /usr/bin/dropbear 2>/dev/null
```

若路径不是 `/usr/sbin/dropbear`，把脚本里的 `command` 改掉。

**启用并启动**：

```sh
/etc/init.d/dropbear enable      # 在 /etc/rc.d/ 下建 S95dropbear 符号链接
ls -l /etc/rc.d/ | grep dropbear
/etc/init.d/dropbear start
sleep 2
netstat -lntp | grep :22
```

**重启验证**：

```sh
reboot
```

重启后等 30~60 秒，在 Mac 上：

```bash
nc -zv 192.168.1.3 22
ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedKeyTypes=+ssh-rsa root@192.168.1.3
```

**回滚**：

```sh
/etc/init.d/dropbear stop
/etc/init.d/dropbear disable
rm -f /etc/init.d/dropbear
cp -a /etc/pam.d/common-auth.bak /etc/pam.d/common-auth
sync
reboot
```

⚠️ 注意：
- 手起的旧进程要先杀掉，避免 22 端口冲突：`killall dropbear` 后再 `start`
- `START=95` 是刻意排在网络就绪之后（`pns` 是 55）；若 wlan0 起得更晚 22 仍会监听 `0.0.0.0`，不影响后续连
- 持久化后 `/etc/dropbear/authorized_keys` 里的出厂公钥**风险升级**（每次开机都可被那个 key 登录），务必清理
- `procd_set_param respawn 3600 5 5` = 1 小时内最多 5 次重启，防崩溃循环

### 16.7 S12 侦察判据 — 已判定（2026-11-01 23:55）

#### 实测答案

| 判据 | 结果 |
| --- | --- |
| fd 里有 `pcmC0D0c` / `pcmC0D0p` | ✅ **两者都有**（fd 13 / fd 5），由 `mipns-xiaomi` 独占 |
| 有 `snd-aloop` 模块 | ❌ 没有（只有 `snd_pcm_oss` / `snd_mixer_oss` / `snd_rawmidi`） |
| `/usr/share/mipns/` 有 input/output 配置节 | ❌ 只有 `vpm/xaudio_engine.conf`（唤醒/VAD 模型路径） |
| Dirac 存在 | ✅ `libDiracAPI_SHARED.so` 2.89 MB |

**结论：S12 的采集与播放都在 `mipns-xiaomi` 进程内部，独占 `pcmC0D0c`（采集）与 `pcmC0D0p`（播放）。**

#### mipns-xiaomi 的 fd 布局（关键证据）

```text
fd 4  -> /dev/snd/timer
fd 5  -> /dev/snd/pcmC0D0p        ← 播放
fd 6  -> /dev/snd/controlC0
fd 13 -> /dev/snd/pcmC0D0c        ← 采集
fd 7  -> /tmp/mipns/pnshelper.fifo
fd 14/15/16/18/21 -> socket（云端 / ubus / 内部）
```

#### 依赖库（`/proc/<pid>/maps`）

```text
/usr/lib/libasound.so.2.0.0
/usr/lib/libDiracAPI_SHARED.so       ← 闭源音效 / AEC
/usr/lib/libmdspeech.so              ← 语音识别
/usr/lib/libvpm.so                   ← 唤醒词 / VAD
/usr/lib/libopus.so.0.5.0            ← opus 编解码
/usr/lib/libaivs-message-util.so
/usr/lib/alsa-lib/libasound_module_rate_speexrate*.so
```

**`libxaudio_engine.so` 不在 maps 里** → 确认 S12A 文档的核心库在此机型不存在。

#### xaudio_engine.conf 内容（S12 版，位于 `/usr/share/mipns/vpm/`）

```ini
[engine opt]
version = s12_v1.1.0-20231026;
[wakeup opt]
version = shadow_version_202301_5.0;
normal_model_path = /usr/share/mipns/vpm/model_all.bin;
[vad opt]
version = model-hires-0605-210105-l16_211015;
model_path = /usr/share/mipns/vpm/;
timeout_num = 600;
timeout_without_begin = 50;
[dnn vad opt]
dnn_vad_model_path = /usr/share/mipns/vpm/dnn_vad_model;
dnn_we_model_path = /usr/share/mipns/vpm/dnn_we_model;
```

> 这是**唤醒词 + VAD 模型配置**，不是音频端点配置。改它只影响唤醒灵敏度/超时，不能换音频源。

#### ALSA 硬件（AMLM8AUDIO）

```text
card 0 [AMLM8AUDIO]
  dev 0: I2S.18 T9015-audio-hifi-0   playback 1 / capture 1   ← 喇叭 + 麦克风
  dev 1: SPDIF.19 dit-hifi-1         playback 1 / capture 1
  dev 2: PCM.20 pcm2bt-pcm-2         playback 1 / capture 1   ← 蓝牙
```

混音器控制（`amixer`）：

```text
'DAC Digital Playback Volume', 'DAC Extra Digital Gain', 'mysoftvol', 'notifyvol',
'Audio i2s mute', 'Audio spdif mute', 'Ext Spk Switch', 'Output Swap',
'Lineout left/right N/P switch', 'bluetooth'
```

**关键**：`Subdevices: 0/1` = dev0 的 subdevice 已占满 → **单进程独占**，第二个 `open` 会失败。

#### 对 home-jarvis 的直接后果

| 方案 | 可行性 |
| --- | --- |
| S12A 的 `LD_PRELOAD` 拦 `libxaudio_engine.so` | ❌ 库不存在 |
| 第二个进程直接 `arecord`/`aplay` hw:0,0 | ❌ 被 mipns 独占（**但停 pns 后可独占，见 §16.9**） |
| 改 `xaudio_engine.conf` 换端点 | ❌ 只是模型路径 |
| `snd-aloop` 虚拟声卡分流 | 🟡 需内核模块，当前未加载 |
| hook `mipns-xiaomi`（ptrace/注入） | 🟡 侵入性强，且 `-r opus32` 是 opus 流，需解 opus |
| **接管 pns**（停 mipns，自己占设备） | ✅ 最干净，**且已验证全双工可行（§16.9）** |
| 走 `/tmp/mipns/pnshelper.fifo` | 🟡 现成 IPC，语义未知需逆向 |

#### 推荐验证路线（关键分水岭）

```sh
# 1) 停 pns，看设备是否释放
/etc/init.d/pns stop
ls -l /proc/*/fd 2>/dev/null | grep -E 'pcmC0D0[cp]' || echo "设备已释放"
cat /proc/asound/card0/pcm0c/sub0/status   # 期望 closed

# 2) 抓 3 秒验证采集
arecord -D hw:0,0 -f S16_LE -r 16000 -c 1 -d 3 /data/test-capture.wav
ls -l /data/test-capture.wav

# 3) 验证播放
aplay -D hw:0,0 /data/test-capture.wav

# 4) 恢复
/etc/init.d/pns start
```

判定：

- `arecord` 出 16k PCM → 可做「接管音频」端点，代价丢 AEC/唤醒
- `Device or resource busy` → 还有进程占着，需查 `lsof` / 相关服务
- 采到静音 → 需 `amixer` 使能麦克风通路

**AEC 现实**：`libDiracAPI_SHARED.so` 闭源，AEC 大概率在其中。接管 ALSA 后 **AEC 失效**，若要「真打断」需退回半双工，或逆向 Dirac API（不建议）。

### 16.8 与 home-jarvis 的对接结论

| 项 | 结论 |
| --- | --- |
| 能否拿到 16k PCM | 🟢 可以（`hw:0,0` 8ch S32 48k，自行降采样；已双工实测） |
| 能否保留原厂唤醒 | ❌ 接管后不能 |
| 能否保留原厂 AEC | ❌ 原厂 AEC 在 `mipns` 里，接管即失去 |
| **能否自建 AEC** | 🟢 **能**，见 [`S12-AEC数据流方案.md`](./S12-AEC数据流方案.md)（ch7 数字环回参考 + SpeexDSP） |
| 推荐接入方式 | 接管 pns + 自建 16k 音频服务（`AUDIO_SOURCE=speaker_endpoint`） |
| 与 ESP32-S3 的关系 | S12 给「好喇叭 + 远场麦」，ESP32 给「全可控」，抽象一致 |

**下一步顺序**：

1. 按 [`S12-AEC数据流方案.md`](./S12-AEC数据流方案.md) §13.2 先验掉两条**未验证项**
   （`DAC Digital Playback Volume` 是否影响 ch7 幅度、ch7 在 DAC 前还是后）
2. 在 Mac 上跑方案 §12 Stage B（AEC 参数标定），再动手写设备侧守护进程
3. 设备侧交叉编译（方案 §9）→ 接 `jarvis`（方案 §7）
4. 通过后再做 dropbear / `S99s12audio` 持久化（因为要 reboot）

> ⚠️ **已完成**：第 1 步两条未验证项已于 11-02 验完（见方案 §4.5）；
> **系统自动更新已于 11-02 禁用**（三层拦截，否则 OTA 会抹掉本文所有接管成果，
> 见方案 §附录 C）。

---

### 16.9 全双工与 AEC 通道实测结论（2026-11-02 13:15）

> 测试方法：`/etc/init.d/pns stop` 后，用 `arecord`(8ch S32 48k) 与 `aplay`(2ch S16 48k)
> **同时**打开 `hw:0,0`，播放 440Hz 正弦与白噪声，录制 5~8 秒，回传 Mac 做频谱/相关分析。

#### 1. 全双工：✅ 成立

```text
arecord 与 aplay 同时持有 hw:0,0 → 两者返回码均为 0
采集 8ch S32_LE / 48000，播放 2ch S16_LE / 48000
```

设备侧 `pns` 停止后 `pcm0c` / `pcm0p` 状态 `closed`，两个进程可**同时**打开。**硬件全双工成立。**

#### 2. 8 通道的身份（白噪声测试，决定性证据）

播放白噪声后，逐通道对源信号做归一化互相关：

| 通道 | 归一化互相关 | 频谱平坦度 | 直流偏置 | 判定 |
| --- | --- | --- | --- | --- |
| **ch7** | **1.0000** | **0.5604**（接近白噪声） | ~0（-0.0000 FS） | ✅ **数字环回参考**（播放 PCM 拷贝，见下方 2026-11-02 修正） |
| ch0 | 0.18 | 0.047 | **-0.052 FS** | 麦克风（声学） |
| ch1 | 0.16 | 0.033 | -0.056 FS | 麦克风 |
| ch2 | 0.15 | 0.045 | -0.059 FS | 麦克风 |
| ch3 | 0.22 | 0.048 | -0.054 FS | 麦克风 |
| ch4 | 0.17 | 0.056 | -0.059 FS | 麦克风 |
| ch5 | 0.22 | 0.042 | -0.056 FS | 麦克风 |
| **ch6** | — | — | 0 | ❌ **恒定 0，未使用** |

**ch7 是纯净的播放通路参考（reference），不是麦克风。** 440Hz 测试中它的谐波失真仅 **-115 dB**，而 ch0-ch5 为 **-35 ~ -48 dB**（声学拾音必然带失真）。

> **2026-11-02 修正（重要）**：后续定量实验证明 ch7 **不是模拟回采，而是播放 PCM 的数字环回拷贝**：
> 幅度比 **-0.02 dB**、峰值逐样本相同、静音段**恰好为 0**、且**不受任何混音器控件影响**。
> 结论「可做 AEC 参考」不变且更强，但连带发现两条硬约束：
> **该机型四个音量控件全部无效**（音量只能软件做）、**硬件只播右声道**（单声道必须复制双声道）。
> 详见 [`S12-AEC数据流方案.md`](./S12-AEC数据流方案.md) §4.5。

#### 3. 声学回声时延（相对 ch7 数字参考，包络互相关）

```text
ch0:  -1 ms      ch1:  -5 ms      ch2: +27 ms
ch3: +26 ms      ch4: +64 ms      ch5:  -4 ms
包络相关系数 0.35 ~ 0.37（存在真实回声耦合）
```

回声时延因通道而异（-5 ~ +64 ms）。这是**房间多径 + 混响 + 箱体共振**在慢包络上的投影，
**不是阵列几何时延**——6 麦口径 ~5~8cm 对应 TDOA 上限只有 `0.08/343 ≈ 0.23ms ≈ 11 samples @48k`，
差 1~2 个数量级。

> ⚠️ 因此这个数字**只能当作「存在真实回声耦合」的证据**，
> **不能**用作 `delay_and_sum` 波束成形的补偿量（会把直达声相位打散）。
> TDOA 必须用 GCC-PHAT 在宽频上现算，详见
> [`S12-AEC数据流方案.md`](./S12-AEC数据流方案.md) §1.1 / §4.8 V2。

#### 4. 采集信号特征（必须知道的工程约束）

| 项 | 实测值 | 影响 |
| --- | --- | --- |
| 直流偏置 | **-0.052 ~ -0.059 FS**（约 -110M LSB） | **必须先做 DC 阻断**，否则动态范围损失 ~6%，且 AEC 会跑偏 |
| 有效幅度 | ch0-5 std ≈ 9~14M LSB（满量程 2147M） | 麦克风增益偏低，约 **-44 dBFS**；AEC 前建议先归一化/提增益 |
| ch6 | 恒为 0 | 该路未接，直接丢弃 |
| `ch7 电平` | 峰 -13.5 dBFS | 参考信号幅度充足，无需额外增益（**保持 ×1.0**，见方案 §4.4） |
| 采样格式 | **S32_LE，8 声道交错** | 走 ALSA 时需 `-f S32_LE -c 8`，自行取通道 |

#### 5. AEC 实施方案（结论）

> 📄 **完整数据流、参数标定、交叉编译与验收标准见独立文档
> [`S12-AEC数据流方案.md`](./S12-AEC数据流方案.md)**（2026-11-02）。
> 下面只是索引，细节以该文档为准。

```text
采集 8ch(S32 48k)  → 丢弃 ch6 → 取 ch0-5
                   → DC 阻断（二阶高通 80Hz，float 域）
                   → 固定增益归一化（S16）
                   → 七路同步重采样 48k → 16k（先降采样，再 AEC）
                   → 取 ch7 作为 far-end reference（同一 buffer，零时延误差）
                   → speex_echo_state_init_mc(160, 3200, nb_mic=6, nb_play=1)
                   → speex_echo_cancellation → 消回声后 6 路
                   → 按 post-AEC SNR 选 1 路
                   → speex_preprocess_run（NS=on / AGC=off / VAD=off）
                   → 16k mono S16 → TCP 上行
```

**关键优势**：ch7 与播放信号在**同一个 ALSA buffer**里，参考信号是**逐样本精确的数字拷贝**（幅度比 -0.02 dB、静音段恰好 0），比 Mac 上"声学回采 + 能量基线"（`jarvis/bargein.py`）强得多。这是 S12 做主端点的重要理由。

**三条硬约束**（违反任一条 AEC 就会失效）：

1. **`DAC Digital Playback Volume` 必须锁死**，用户音量走软件乘性缩放 —— 否则小音量时参考信号掉进噪声底
2. **先降到 16k 再做 AEC** —— 48k 下滤波器长度是 3 倍，FFT 开销 9 倍，4×A53 吃不下
3. **mic 与 ref 用完全相同的重采样参数、每个 period 相同样本数** —— 否则 7 路相对延迟不为 0，对齐被破坏

**仍需自研的部分**：

1. 6 路选 1 的路上策（V1 按 post-AEC SNR；V2 才做 GCC-PHAT 波束成形）
2. 重采样 48k → 16k（设备有 `libspeexdsp` 的 `speex_resampler_*`，无需 ALSA 插件）
3. 交叉编译 aarch64 静态二进制（设备无 gcc / 无 python，glibc 2.19，见方案 §9）

#### 6. 复现命令（设备侧）

```sh
/etc/init.d/pns stop

# 生成并上传测试音（Mac 侧）
ffmpeg -y -f lavfi -i "anoisesrc=d=6:c=white:a=0.3:r=48000" -ac 2 -c:a pcm_s16le noise.wav
cat noise.wav | ssh root@<IP> '/bin/cat > /data/noise.wav'

# 双工录制（设备侧；注意 sleep 用绝对路径或 shell 内建）
(arecord -D hw:0,0 -f S32_LE -r 48000 -c 8 -d 8 /data/rec.wav &) 
aplay -D hw:0,0 /data/noise.wav
# 等 arecord 退出后回传分析
```

> ⚠️ 坑：设备 busybox 的 `sleep` 不接受小数（`sleep 0.5` 报 `invalid number`），
> 且 `scp` / `cat` 在精简 SSH 环境下不在 PATH，需用 `/bin/cat` 管道传输。

---

## 17. 实测时间线（2026-11-01 → 11-02）

按发生顺序，便于后续接手时判断「哪些结论是验证过的」。

| 时间 | 事件 | 结论 |
| --- | --- | --- |
| 11-01 21:26 | 确定 `J28` 为调试串口，万用表确认最右为 GND | 接线方案成立 |
| 11-01 21:49 | `/dev/tty.usbserial-3110` 出现 | CH340 驱动正常 |
| 11-01 21:53 | `screen 115200` + Enter → `S12 login:` | 串口双向通 |
| 11-01 22:01 | `root` + `md5(SN+盐)` 前 14 位 → `dsa verify failed` | **旧密码算法失效** |
| 11-01 22:15 | 丝印 `DKSND-S12-ECHO-AB-20170210` | **不是 S12A，是 S12** |
| 11-01 22:19 | 断电重插后 TTL 无输出、无启动声 | TTL 接线干扰供电 |
| 11-01 22:21 | 拔掉 3 根线后启动声/灯正常 | 确认是接线问题 |
| 11-01 22:32 | `f` → `Enter` 进 failsafe `root@(none):/#` | **可进系统** |
| 11-01 22:34 | `/proc/mtd` 六个分区，system0/1 各 96 MiB | 分区表与 S12A 不同 |
| 11-01 22:38 | `ubi0:rootfs` → mtd4=system1，UBIFS rw | 根可写 |
| 11-01 22:41 | `version` → S12 / 1.74.18 / 2023-12-06 | 版本坐实 |
| 11-01 22:47 | 改 `inittab` 为 `ash --login` 后重启 | **持久 root shell** |
| 11-01 23:19 | dropbear `Bad buf_getptr` | host key 是 0 字节 |
| 11-01 23:21 | 重建 rsa key → `0.0.0.0:22 LISTEN` | SSH 起来 |
| 11-01 23:32 | PAM 改动 + `libpam.so.0` 确认 | 认证链路生效 |
| 11-01 23:41 | 音频侦察 round 1 | **`libxaudio_engine.so` 不存在** |
| 11-01 23:55 | 音频侦察 round 2（fd / maps / ALSA） | mipns 独占 `pcmC0D0c/p` |
| 11-02 11:54 | 归档本节 | 等待接管 pns 验证 |

### 关键「已验证 vs 待验证」分隔

**已验证（可直接依赖）**

- 串口接线（J28：右 GND、另两根交叉）
- 进 failsafe 与正常系统 root shell
- `/etc/inittab` 改动持久有效
- SSH 可用（临时进程）
- S12 无 `libxaudio_engine.so`、无 `snd-aloop`
- `mipns-xiaomi` 独占 `pcmC0D0c` 与 `pcmC0D0p`

**待验证（不要当结论用）**

- 接管后麦克风是否需要额外 `amixer` 使能
- 改 `DAC Digital Playback Volume` 是否改变 ch7 幅度（决定音量纪律，见方案 §13.2 #1）
- ch7 是 DAC 前还是 DAC 后的回采（决定 AEC 理论上限，见方案 §13.2 #2）
- AEC 实际 CPU 占用（估算单核 35%，需实测）
- system0（mtd3）的版本
- dropbear 持久化后能否稳定自启

> 已有结论的项已从本清单移出：全双工成立、ch7 为数字环回参考、16k PCM 可得，
> 见 §16.9 与 [`S12-AEC数据流方案.md`](./S12-AEC数据流方案.md)。
> 音量控件与右声道两条硬约束也已于 2026-11-02 验证（同文档 §4.5）。

### 恢复现场的最短路径（明天继续）

```sh
# 1) 串口
screen /dev/tty.usbserial-3120 115200      # 先 ls /dev/tty.* 确认编号

# 2) 若 SSH 已断（重启过），串口里手起
dropbear -p 22

# 3) 从 Mac 直连（公钥已配）
ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedKeyTypes=+ssh-rsa root@192.168.1.3
```

> 音箱 IP 为 `192.168.1.3`（DHCP，重启后可能变；以串口 `ifconfig` 为准）

