(function(root){
function build(){return {
version:'image-face-only-video-master-v4',clothing:'video',background:'video',
prompt:`IMAGE REFERENCE = <Picture 1>. VIDEO REFERENCE = <Video 1>.
Generate photorealistic footage of the selected video interval with the facial identity from <Picture 1>.

<Picture 1> supplies FACIAL IDENTITY ONLY: preserve its facial structure, eyes, nose, mouth, jaw and natural complexion across the source head angles. Do not copy the photo's clothes, pose, hairstyle, expression, background or lighting. Adapt the face naturally to the video's lighting and expression without blending the two identities.

<Video 1> supplies EVERYTHING ELSE. Reproduce its visible clothing exactly: same garments, construction, fit, coverage, colors, materials, patterns and accessories. Do not redesign or substitute the outfit. Preserve the video's hairstyle, body pose, setting, lighting, camera position and framing.

Follow <Video 1> as the temporal reference: preserve action order, timing, pose changes, hand contacts, gaze, expressions and camera motion. Match the start and end of the selected interval. Keep a stationary camera stationary. Preserve source repeats or cuts only when they actually occur; do not add actions or a new story.

Maintain coherent anatomy, stable facial features, consistent clothing and natural motion. Do not hide errors with added blur, smoothing or camera movement. Do not copy app interface overlays, captions or watermarks.`,
negative:'blended faces, facial identity drift, photo outfit, substituted clothing, invented patterns, invented accessories, anatomy errors, added loops, invented actions, background flicker, added camera motion, text, watermark'
};}
root.H3ReferenceContract={build};
if(typeof module!=='undefined')module.exports={build};
})(typeof window==='undefined'?globalThis:window);
