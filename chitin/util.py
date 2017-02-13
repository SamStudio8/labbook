import getpass
import hashlib
import json
import os
import sys
import time
import warnings

from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

import record
from cmd import attempt_parse_type, attempt_integrity_type

def get_file_record(path):
    path = os.path.abspath(path)
    try:
        item = record.Item.query.filter(record.Item.path==path)[0]
    except IndexError:
        return None
    return item

def get_status(path, cmd_str=""):
    abspath = os.path.abspath(path)

    h = 0
    status = '?'
    last_h = 0
    path_record = get_file_record(abspath)
    if os.path.exists(abspath):
        if os.path.isfile(abspath):
            h = hashfile(abspath)
        elif os.path.isdir(abspath):
            h = hashfiles([os.path.join(abspath,f) for f in os.listdir(abspath) if os.path.isfile(os.path.join(abspath,f))])

        if path_record:
            try:
                last_h = path_record.get_last_digest()
            except IndexError:
                pass

            # Path exists and we knew about it
            if path_record.get_last_digest() != h:
                status = "M"
            else:
                status = "U"
        else:
            # Path exists but it is a surprise
            status = "C"
    elif path_record:
        last_h = path_record.get_last_digest()
        status = "D"

    return (status, h, last_h)

def add_event_group(run_uuid):
    run = None
    try:
        run = record.Run.query.filter(record.Run.uuid==str(run_uuid))[0]
    except IndexError as e:
        pass
    event_group = record.EventGroup(run=run)
    record.db.session.add(event_group)
    record.db.session.commit()
    return event_group.id


def add_file_record(path, cmd_str, status=False, parent=None, meta=None, uuid=None, group_id=None):
    item = get_file_record(path)
    if not item:
        item = record.Item(path)
        record.db.session.add(item)
        record.db.session.commit()

    event = None
    if uuid:
        try:
            event = record.Event.query.filter(record.Event.uuid==str(uuid))[0]
        except IndexError as e:
            pass

    if not event:
        group = None
        if group_id:
            group = record.EventGroup.query.get(group_id)
        event = record.Event(cmd_str, uuid, group)
        record.db.session.add(event)

        if meta:
            for mcat in meta:
                for key in meta[mcat]:
                    datum = record.Metadatum(event, mcat, key, meta[mcat][key])
                    record.db.session.add(datum)

    itemevent = record.ItemEvent(item, event, status)
    record.db.session.add(itemevent)

    if status != 'D':
        #NOTE This is a pretty hacky way of getting around accidentally handling
        #     files that have been deleted.
        f_meta = attempt_parse_type(item.path)
        if f_meta:
            for key in f_meta:
                datum = record.Metadatum(event, item.path, key, f_meta[key])
                record.db.session.add(datum)

    record.db.session.commit()

def check_integrity_set(path_set, file_tokens=None, skip_check=False):
    if not file_tokens:
        file_tokens = []
    failed = []
    for item in path_set:
        item = os.path.abspath(item)
        if check_integrity(item, is_token=item in file_tokens, skip_check=skip_check):
            failed.append(item)
        if skip_check:
            continue

        if os.path.isdir(item):
            for subitem in os.listdir(item):
                i_abspath = os.path.join(item, subitem)
                if os.path.isdir(i_abspath):
                    if check_integrity(i_abspath):
                        failed.append(i_abspath)

                    for subsubitem in os.listdir(i_abspath):
                        j_abspath = os.path.join(i_abspath, subsubitem)
                        if j_abspath in path_set:
                            continue
                        if os.path.isfile(j_abspath):
                            if check_integrity(j_abspath, is_token=j_abspath in file_tokens):
                                failed.append(j_abspath)
                else:
                    #TODO Do we want to keep a record of the files of subfolders?
                    if i_abspath in path_set:
                        continue
                    if check_integrity(i_abspath, is_token=i_abspath in file_tokens):
                        failed.append(i_abspath)
    return sorted(failed)

def check_integrity(path, is_token=False, skip_check=False):
    abspath = os.path.abspath(path)
    broken_integrity = False
    broken_rules = {}

    if skip_check:
        if os.path.exists(abspath):
            if os.path.isfile(abspath):
                broken_rules = attempt_integrity_type(abspath)
    else:
        path_record = get_file_record(abspath)
        if os.path.exists(abspath):
            if os.path.isfile(abspath):
                broken_rules = attempt_integrity_type(abspath)
                h = hashfile(abspath)
            elif os.path.isdir(abspath):
                h = hashfiles([os.path.join(abspath,f) for f in os.listdir(abspath) if os.path.isfile(os.path.join(abspath,f))])

            if path_record:
                # Path exists and we knew about it
                if path_record.get_last_digest() != h:
                    add_file_record(abspath, "MODIFIED by (?)")
                    broken_integrity = True
            else:
                # Path exists but it is a surprise
                add_file_record(abspath, "CREATED by (?)")
                broken_integrity = True
        elif path_record:
            add_file_record(abspath, "DELETED by (?)")
            broken_integrity = True

    #TODO I don't want this here but I can't be bothered to move it right now
    if is_token:
        for rule, result in broken_rules.items():
            if not result and result is not None:
                print "[WARN] %s %s" % (path, rule[1])

    return broken_integrity

def parse_tokens(fields, env_vars, ignore_parents=False):
    dirs_l = []
    file_l = []
    for field_i, field in enumerate(fields):
        for env_k in env_vars:
            if '$' + env_k in field:
                field = field.replace('$' + env_k, str(env_vars[env_k]))
                fields[field_i] = field

        had_semicolon = False
        if field[-1] == ";":
            had_semicolon = True
            field = field.replace(";", "")
        abspath = os.path.abspath(field)

        # Does the path exist? We might want to add its parent directory
        if os.path.exists(abspath):
            if had_semicolon:
                fields[field_i] = abspath + ';' # Update the command to use the full abspath
            else:
                fields[field_i] = abspath # Update the command to use the full abspath

            if not ignore_parents:
                dirs_l.append(os.path.dirname(abspath))
        else:
            potential_dir = os.path.dirname(abspath)
            if os.path.exists(potential_dir) and not ignore_parents:
                dirs_l.append(potential_dir)
            continue

        ### Files
        if os.path.isfile(abspath):
            file_l.append(abspath)

            if not ignore_parents:
                dirs_l.append(os.path.dirname(abspath))

        ### Dirs
        elif os.path.isdir(abspath):
            dirs_l.append(abspath)

            for item in os.listdir(abspath):
                i_abspath = os.path.join(abspath, item)
                if os.path.isdir(i_abspath):
                    dirs_l.append(i_abspath)
                else:
                    #TODO Do we want to keep a record of the files of subfolders?
                    pass
    return {
        "fields": fields,
        "files": set(file_l),
        "dirs": set(dirs_l),
    }


def hashfile(path, halg=hashlib.md5, bs=65536):
    f = open(path, 'rb')
    buff = f.read(bs)
    halg = halg()
    halg.update(buff)
    while len(buff) > 0:
        buff = f.read(bs)
        halg.update(buff)
    f.close()
    return halg.hexdigest()

def hashfiles(paths, halg=hashlib.md5, bs=65536):
    tot_halg = halg()
    for path in sorted(paths):
        tot_halg.update(hashfile(path, halg=halg, bs=bs))
    return tot_halg.hexdigest()

def check_status_path_set(path_set):
    dir_statii = {}
    file_statii = {}
    dir_codes = {"C": 0, "M": 0, "D": 0, "U": 0}
    file_codes = {"C": 0, "M": 0, "D": 0, "U": 0}
    codes = {"C": 0, "M": 0, "D": 0, "U": 0}
    hashes = {}

    for item in path_set:
        item = os.path.abspath(item)
        if not os.path.exists(item):
            stat = get_status(item)
            file_statii[item] = stat[0]
            hashes[item] = (stat[1], stat[2])

        if os.path.isdir(item):
            stat = get_status(item)
            dir_statii[item] = stat[0]
            hashes[item] = (stat[1], stat[2])

            for subitem in os.listdir(item):
                i_abspath = os.path.join(item, subitem)
                if os.path.isdir(i_abspath):
                    stat = get_status(i_abspath)
                    dir_statii[i_abspath] = stat[0]
                    hashes[i_abspath] = (stat[1], stat[2])
                else:
                    #TODO Do we want to keep a record of the files of untargeted subfolders?
                    stat = get_status(i_abspath)
                    file_statii[i_abspath] = stat[0]
                    hashes[i_abspath] = (stat[1], stat[2])
        elif os.path.isfile(item):
            stat = get_status(item)
            file_statii[item] = stat[0]
            hashes[item] = (stat[1], stat[2])

    for s in dir_statii.values():
        dir_codes[s] += 1
        codes[s] += 1
    for s in file_statii.values():
        file_codes[s] += 1
        codes[s] += 1

    return {
        "dirs": dir_statii,
        "files": file_statii,
        "d_codes": dir_codes,
        "f_codes": file_codes,
        "codes": codes,
        "hashes": hashes,
    }

def register_experiment(path, create_dir=False):
    exp = record.Experiment(path)
    record.db.session.add(exp)
    record.db.session.commit()

    if create_dir:
        try:
            os.mkdir(exp.get_path())
        except:
            #TODO would be nice if we could distinguish between OSError 13 (permission) etc.
            print("[WARN] Encountered trouble creating %s" % path)
    return exp

def register_run(exp_uuid, create_dir=False, meta=None):
    try:
        exp = record.Experiment.query.filter(record.Experiment.uuid==str(exp_uuid))[0]
    except IndexError as e:
        return None

    run = record.Run(exp)
    record.db.session.add(run)

    if meta:
        for key in meta:
            datum = record.RunMetadatum(run, key, meta[key])
            record.db.session.add(datum)
    record.db.session.commit()

    if create_dir:
        try:
            os.mkdir(run.get_path())
        except:
            #TODO would be nice if we could distinguish between OSError 13 (permission) etc.
            print("[WARN] Encountered trouble creating %s" % path)
    return run

def archive_experiment(exp_uuid, tar_path=None, manifest=True, new_root=None):
    import tarfile
    exp = record.Experiment.query.get(exp_uuid)
    if not exp:
        return None

    def translate_tarinfo(info):
        info.name = os.path.join(exp.uuid, "".join(info.name.split(exp.uuid)[1:])[1:])
        if new_root:
            info.name = os.path.join(new_root, info.name)
        return info

    if tar_path is None:
        tar_path = os.path.join(exp.get_path(), exp.uuid + ".tar.gz")

    tar = tarfile.open(tar_path, "w|gz")
    tar.add(exp.get_path(), filter=translate_tarinfo)

    tar.close()

    return tar_path

def generate_experiment_manifest(exp_uuid, dest=None):
    exp = record.Experiment.query.get(exp_uuid)
    if not exp:
        return None

    if not dest:
        dest = os.path.join(exp.get_path(), exp.uuid + ".manifest")
    dest_fh = open(dest, "w")

    for r in exp.runs:
        dest_fh.write(
            ("%s\t" % r.uuid) + "\t".join([m.value for m in r.rmeta]) + "\n"
        )
    dest_fh.close()


def copy_experiment_archive(exp_uuid, hostname, ssh_config_path=None, dest=None, new_root=None, manifest=False):
    import paramiko

    tar_path = archive_experiment(exp_uuid, new_root=new_root, manifest=manifest)

    pw = getpass.getpass()

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    config = paramiko.SSHConfig()
    if not ssh_config_path:
        ssh_config_path = os.path.expanduser("~/.ssh/config")

    config.parse(open(ssh_config_path))

    connect_config = dict(config.lookup(hostname))
    if 'user' in connect_config:
        connect_config['username'] = connect_config['user']
        del connect_config['user']
    if 'proxycommand' in connect_config:
        connect_config['sock'] = paramiko.ProxyCommand(connect_config['proxycommand'])
        del connect_config['proxycommand']

    connect_config["look_for_keys"] = False
    connect_config["allow_agent"] = False
    connect_config["password"] = pw
    ssh.connect(**connect_config)

    sftp = ssh.open_sftp()
    if dest is not None:
        sftp.chdir(dest)
    print(sftp.put(tar_path, os.path.basename(tar_path), confirm=True))

    if dest is None:
        dest = "~"
    stdin, stdout, stderr = ssh.exec_command('tar -xvPf ' + os.path.join(dest, os.path.basename(tar_path)))
    print("".join(stdout.readlines()))
    ssh.close()


#TODO(samstudio8) Find a non-garbage way of finding a nice default truetype font
def watermark_experiment_image(exp_uuid, image_path, font_path="/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf", name=None):
    exp = record.Experiment.query.get(exp_uuid)
    if not exp or not os.path.exists(exp.get_path()):
        print("[WARN] Could not create watermarked experiment image.")
        return

    font = ImageFont.truetype(font_path, 36)
    img = Image.open(image_path)
    width, height = img.size

    # Create a new image with some space at the bottom for metadata
    # color=(0,0,0) somewhat assumes RGB so might implode
    new_img = Image.new(img.mode, (width, height+48), color=(0,0,0))
    new_img.paste(img, (0,0))

    # Draw the UUID onto the image
    draw = ImageDraw.Draw(new_img)
    t_msg = exp_uuid
    msg_w, msg_h = draw.textsize(t_msg, font=font)
    draw.text(((width-msg_w)/2, height), t_msg, font=font)

    # Save the image
    if not name:
        name = datetime.now().strftime("%Y-%m-%d_%H:%M:%S") + ".png"
    new_img.save(os.path.join(exp.get_path(), name))
