"""Joint visual review of pour and verification files from one experiment."""
from pathlib import Path
import json

from .. import ai_editor_contract as identity
from ..media.temporal_samples import validate as validate_samples,guard_outputs
from ..media import action_frames
from ..selection import ai_editor
from ..selection.experiment_contract import validate_pair,adjacency_hint,pair_sample_ids,focus_frame_ids
from .purpose_review import policy,save

PROTOCOL="weipin-same-experiment-pair-v1"


def input_binding(manifest,members,sheet):
    """Bind ordered sources, frame mapping and windows to the submitted pixels."""
    return {"task_id":manifest["task_id"],"rules":identity.rules_identity(manifest),
        "product":manifest["product"],"sheet_sha256":identity.file_sha256(sheet),
        "members":[{"part":m["part"],"samples_sha256":identity.digest(m["samples"]),
            "window_frame_ids":m["window_frame_ids"],"visible_frame_ids":m["visible_frame_ids"]}for m in members]}


def review(manifest,first,second,first_window,second_window,output,*,ffmpeg="ffmpeg",ffprobe="ffprobe",font_path,
           focus_ranges_us=None):
    from PIL import Image,ImageDraw,ImageFont
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    allowed={str(Path(p).resolve())for p in manifest["material_sources"]}
    for samples in (first,second):
        validate_samples(samples)
        if samples["original"]["path"]not in allowed:raise ValueError("EXPERIMENT_PAIR_OUTSIDE_TASK")
        guard_outputs(samples,output,(".jpg",".prompt.txt",".request.json",".pair.json",".binding.json"))
    font=ImageFont.truetype(str(font_path),20)
    members=[];visible={};tiles=[]
    for part,samples,window in (("A",first,first_window),("B",second,second_window)):
        a,b=window
        if type(a)is not int or type(b)is not int or not 0<=a<b<len(samples["frames"]):
            raise ValueError("EXPERIMENT_PAIR_WINDOW_INVALID")
        focus=focus_frame_ids(samples,a,b,(focus_ranges_us or {}).get(part))
        ids=pair_sample_ids(samples,a,b,focus)
        metadata=action_frames.probe(samples["source"]["path"],output.parent,ffprobe=ffprobe)
        frames=action_frames.extract(samples["source"]["path"],metadata,
            [samples["frames"][i]["source_frame"]for i in ids],output.parent,ffmpeg=ffmpeg,width=270)
        member={"part":part,"samples":samples,"window_frame_ids":[a,b],"visible_frame_ids":ids}
        if focus:member["focus_frame_ids"]=focus
        members.append(member);tiles.append((part,ids,frames))
        visible[part]=set(ids)
    columns=4 if all(len(t[1])<=8 for t in tiles) else 6
    rows=[-(-len(t[1])//columns) for t in tiles]
    canvas=Image.new("RGB",(columns*270,sum(rows)*510),"#222222");draw=ImageDraw.Draw(canvas)
    top=0
    for (part,ids,frames),count in zip(tiles,rows):
        for j,(index,frame)in enumerate(zip(ids,frames)):
            im=Image.open(frame["path"]).convert("RGB");im.thumbnail((270,480))
            x=j%columns*270;y=top+j//columns*510
            canvas.paste(im,(x,y+30));draw.text((x+6,y+4),f"{part} frame {index}",font=font,fill="white")
        top+=count*510
    sheet=output.with_suffix(".jpg");canvas.save(sheet,quality=95)
    binding=input_binding(manifest,members,sheet)
    binding_path=output.with_suffix(".binding.json");save(binding_path,binding)
    # The observer never receives adjacency, filenames, old verdicts or a target
    # narration duration. Pairing requires actual visual correspondence.
    prompt=policy(manifest)+"""联合核对两个源文件的画面：上半A为倒液吸收候选，下半B为纸巾按压验证候选。
用户允许跨文件，重点是B是否验证A这次实验，不能换成别次实验。允许合理换机位与吸液后的状态扩散，
不要求跨文件连续录像证明；但必须逐项看清商品、实验承载物/布置、巾体湿润状态和验证对应关系。
场景或承载物明显不同、巾体状态冲突则mismatch；无法确认关联则uncertain。文件相邻不属于本次证据。
不要把图片拼在一起视为已证实关联。只报告实际看见的帧编号，不能从文案补齐倒液或纸巾动作。
只输出JSON {"verdict":"matched|mismatch|uncertain","comparisons":{
"product":{"status":"consistent|conflict|uncertain","observed":"实际A/B对照"},
"setup":{"status":"consistent|conflict|uncertain","observed":"承载物及布置"},
"material_state":{"status":"consistent|conflict|uncertain","observed":"液体分布、巾体状态是否能接上"},
"verification_correspondence":{"status":"consistent|conflict|uncertain","observed":"按压是否对应这次吸液"}},
"phases":[{"part":"A|B","kind":"pour|absorb|press|lift|result","first_frame":0,"last_frame":1,"observed":"事实"}],
"limitations":"看不清或不能确认的内容"}。
pour/absorb属于A，press/lift/result属于B；不存在的阶段不填，result从提起翻转完成后可读处开始。
白色纸巾只支持无明显湿痕的外观，不能推断完全没有水。"""+json.dumps({"product":manifest["product"],"shown_frame_ids":{k:sorted(v)for k,v in visible.items()},"input_binding_sha256":identity.digest(binding)},ensure_ascii=False)
    row=ai_editor._call(prompt,output,sheet)
    validate_pair(row,visible)
    for samples in(first,second):validate_samples(samples)
    artifact={"protocol":PROTOCOL,"task_id":manifest["task_id"],"rules":identity.rules_identity(manifest),
        "context":{"product":manifest["product"],"category":identity.rules_identity(manifest)["category"]},
        "members":members,"observation":row,"sheet":identity.source_identity(sheet),
        "input_binding":{"file":identity.source_identity(binding_path),"sha256":identity.file_sha256(binding_path)},
        "retrieval_hint":adjacency_hint(first["original"]["path"],second["original"]["path"]),
        "formal_delivery_allowed":False,"method":"joint actual-frame samples; pairing is not final film acceptance"}
    save(output.with_suffix(".pair.json"),artifact)
    return artifact
