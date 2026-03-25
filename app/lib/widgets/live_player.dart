import 'package:flutter/material.dart';
import 'package:flutter_inappwebview/flutter_inappwebview.dart';

/// Live video player using WebView + go2rtc MSE WebSocket.
///
/// Uses the browser's native MSE (Media Source Extensions) to play go2rtc's
/// fMP4 stream. Works with any codec the browser supports (H.264, HEVC, etc.)
/// because Chromium/WebView2 handles decoding natively.
class LivePlayer extends StatefulWidget {
  final String wsUrl;
  final int rotation;
  final BoxFit fit;

  const LivePlayer({
    super.key,
    required this.wsUrl,
    this.rotation = 0,
    this.fit = BoxFit.contain,
  });

  @override
  State<LivePlayer> createState() => _LivePlayerState();
}

class _LivePlayerState extends State<LivePlayer> {
  InAppWebViewController? _controller;

  String _buildHtml(String wsUrl) {
    // Object-fit mapped from BoxFit
    final objectFit = widget.fit == BoxFit.cover ? 'cover' : 'contain';
    return '''<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
*{margin:0;padding:0}
body{background:#000;overflow:hidden;width:100vw;height:100vh}
video{width:100%;height:100%;object-fit:$objectFit}
</style></head><body>
<video id="v" autoplay muted playsinline></video>
<script>
const wsUrl="$wsUrl";
const video=document.getElementById("v");
let ms,sb,queue=[];

function connect(){
  if(ms&&ms.readyState==="open"){try{ms.endOfStream()}catch(e){}}
  ms=null;sb=null;queue=[];

  const ws=new WebSocket(wsUrl);
  ws.binaryType="arraybuffer";

  ws.onopen=()=>{ws.send(JSON.stringify({type:"mse"}))};

  ws.onmessage=(ev)=>{
    if(typeof ev.data==="string"){
      let msg;
      try{msg=JSON.parse(ev.data)}catch{return}
      if(msg.type!=="mse"||!msg.value)return;
      ms=new MediaSource();
      video.src=URL.createObjectURL(ms);
      ms.addEventListener("sourceopen",()=>{
        try{
          sb=ms.addSourceBuffer(msg.value);
          sb.mode="segments";
          sb.addEventListener("updateend",()=>{
            flush();
            if(sb&&!sb.updating&&sb.buffered.length>0){
              const end=sb.buffered.end(sb.buffered.length-1);
              const trimTo=end-30;
              if(trimTo>sb.buffered.start(0)){
                try{sb.remove(sb.buffered.start(0),trimTo)}catch(e){}
              }
            }
          });
          flush();
          video.play().catch(()=>{});
        }catch(e){}
      },{once:true});
    }else{
      if(sb&&!sb.updating){
        try{sb.appendBuffer(ev.data)}catch{queue.push(ev.data)}
      }else{
        queue.push(ev.data);
        if(queue.length>100)queue.splice(0,queue.length-50);
      }
    }
  };

  ws.onclose=()=>{setTimeout(connect,3000)};
  ws.onerror=()=>{ws.close()};
}

function flush(){
  if(sb&&!sb.updating&&queue.length>0){
    try{sb.appendBuffer(queue.shift())}catch(e){}
  }
  if(video.buffered.length>0){
    const end=video.buffered.end(video.buffered.length-1);
    if(end-video.currentTime>5)video.currentTime=end-0.5;
  }
}

connect();
</script></body></html>''';
  }

  @override
  void didUpdateWidget(LivePlayer oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.wsUrl != widget.wsUrl) {
      _controller?.loadData(
        data: _buildHtml(widget.wsUrl),
        mimeType: 'text/html',
        encoding: 'utf-8',
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final rot = widget.rotation;
    final isRotated = rot == 90 || rot == 270;

    Widget webview = InAppWebView(
      initialData: InAppWebViewInitialData(
        data: _buildHtml(widget.wsUrl),
        mimeType: 'text/html',
        encoding: 'utf-8',
      ),
      initialSettings: InAppWebViewSettings(
        mediaPlaybackRequiresUserGesture: false,
        allowsInlineMediaPlayback: true,
        transparentBackground: true,
        disableHorizontalScroll: true,
        disableVerticalScroll: true,
        supportZoom: false,
      ),
      onWebViewCreated: (controller) {
        _controller = controller;
      },
    );

    if (rot != 0) {
      webview = Transform.rotate(
        angle: rot * 3.14159265 / 180,
        child: isRotated
            ? Transform.scale(scale: 0.5625, child: webview)
            : webview,
      );
    }

    return webview;
  }
}
