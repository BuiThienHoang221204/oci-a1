"""Thử tạo instance A1 (Always Free) ở Singapore cho tới khi Oracle cấp được máy.

Chạy: python create_a1.py            (thử mãi, 5 phút/lần)
      python create_a1.py --once     (thử đúng một lần — GitHub Actions dùng chế độ này)
Mã thoát: 0 = chưa có máy (thử lại lượt sau), 3 = máy ĐÃ SẴN SÀNG, 2 = lỗi cần người xem.
"""
import datetime
import os
import pathlib
import sys
import time

import oci

HERE = pathlib.Path(__file__).parent
LOG = HERE / "create_a1.log"

NAME = "careelot-api"
AD = "Wqrj:AP-SINGAPORE-1-AD-1"
SUBNET = "ocid1.subnet.oc1.ap-singapore-1.aaaaaaaabxsblerdjlviviyupgawzz7w6m4l6ytrjaci5lhxr2jzqom5lmwq"
IMAGE = "ocid1.image.oc1.ap-singapore-1.aaaaaaaaw75ef2chh5goomskvdznqz36cadczqelqvndbqcozc5icyig57za"
OCPUS = 2
MEMORY_GB = 12
BOOT_GB = 100
FREE_STORAGE_GB = 200
INTERVAL_S = 300

READY, NEEDS_HUMAN = 3, 2


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def summary(msg):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


def ssh_public_key():
    key = os.environ.get("SSH_PUBLIC_KEY", "").strip()
    return key or (pathlib.Path.home() / ".ssh" / "oracle.key.pub").read_text().strip()


def call(fn, *args, **kwargs):
    """Thử lại khi 401: API key mới cần vài phút để lan tới mọi dịch vụ."""
    for _ in range(10):
        try:
            return fn(*args, **kwargs)
        except oci.exceptions.ServiceError as e:
            if e.status != 401:
                raise
            time.sleep(20)
    raise RuntimeError("Vẫn 401 sau nhiều lần thử — kiểm lại API key")


cfg = oci.config.from_file()
tenancy = cfg["tenancy"]
compute = oci.core.ComputeClient(cfg)
network = oci.core.VirtualNetworkClient(cfg)
storage = oci.core.BlockstorageClient(cfg)


def existing():
    for inst in call(compute.list_instances, tenancy, display_name=NAME).data:
        if inst.lifecycle_state not in ("TERMINATED", "TERMINATING"):
            return inst
    return None


def storage_used():
    boots = call(storage.list_boot_volumes, availability_domain=AD, compartment_id=tenancy).data
    vols = call(storage.list_volumes, compartment_id=tenancy).data
    return sum(v.size_in_gbs for v in boots + vols if v.lifecycle_state != "TERMINATED")


def public_ip(inst_id):
    for att in call(compute.list_vnic_attachments, tenancy, instance_id=inst_id).data:
        vnic = call(network.get_vnic, att.vnic_id).data
        if vnic.public_ip:
            return vnic.public_ip
    return None


def launch(boot_gb):
    details = oci.core.models.LaunchInstanceDetails(
        compartment_id=tenancy,
        availability_domain=AD,
        display_name=NAME,
        shape="VM.Standard.A1.Flex",
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=OCPUS, memory_in_gbs=MEMORY_GB),
        source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=IMAGE, boot_volume_size_in_gbs=boot_gb),
        create_vnic_details=oci.core.models.CreateVnicDetails(subnet_id=SUBNET, assign_public_ip=True),
        metadata={"ssh_authorized_keys": ssh_public_key()},
    )
    return call(compute.launch_instance, details).data


def done(inst):
    log(f"Instance {inst.display_name} ({inst.lifecycle_state}) — chờ RUNNING...")
    inst = oci.wait_until(compute, compute.get_instance(inst.id), "lifecycle_state", "RUNNING", max_wait_seconds=900).data
    ip = public_ip(inst.id)
    log(f"XONG: {inst.display_name} RUNNING, IP công khai = {ip}")
    (HERE / "NEW_INSTANCE.txt").write_text(f"{inst.id}\n{ip}\n", encoding="utf-8")
    summary(f"## Máy A1 đã sẵn sàng\n\n`{NAME}` — IP công khai **{ip}**\n\nHãy TẮT workflow `oci-a1` trong tab Actions.")
    return READY


def main():
    once = "--once" in sys.argv
    while True:
        try:
            return run(once)
        except oci.exceptions.RequestException as e:
            log(f"Lỗi mạng tới Oracle — {str(e)[:120]}")
            if once:
                return 0
            time.sleep(INTERVAL_S)


def run(once):
    found = existing()
    if found:
        return done(found)

    used = storage_used()
    boot_gb = min(BOOT_GB, FREE_STORAGE_GB - used)
    log(f"Ổ đĩa đang dùng {used}GB / {FREE_STORAGE_GB}GB miễn phí → boot volume {boot_gb}GB")
    if boot_gb < 50:
        log("Không đủ ổ đĩa miễn phí cho boot volume >= 50GB — dừng.")
        return NEEDS_HUMAN

    attempt = 0
    while True:
        attempt += 1
        try:
            return done(launch(boot_gb))
        except oci.exceptions.ServiceError as e:
            msg = (e.message or "")[:160]
            if "capacity" in msg.lower():
                log(f"Lần {attempt}: hết máy A1 ({e.status})")
            elif e.status == 429:
                log(f"Lần {attempt}: bị giới hạn tần suất (429)")
                if not once:
                    time.sleep(INTERVAL_S)
            elif e.code == "LimitExceeded" or "limit" in msg.lower():
                log(f"Lần {attempt}: vượt hạn mức tài khoản — dừng. {e.code}: {msg}")
                return NEEDS_HUMAN
            else:
                log(f"Lần {attempt}: lỗi khác {e.status} {e.code}: {msg}")
            if existing():
                return done(existing())
        except oci.exceptions.RequestException as e:
            log(f"Lần {attempt}: lỗi mạng tới Oracle, thử lại lượt sau — {str(e)[:120]}")
        if once:
            return 0
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main())
