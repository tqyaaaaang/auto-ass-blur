// Coarse C ABI: Python never traverses borrowed libass image chains.
#include <ass/ass.h>
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <exception>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>
#include <dlfcn.h>

namespace {
thread_local std::string error;
struct Budget {
    size_t limit, used=0, peak=0;
    std::mutex mutex;
    explicit Budget(size_t n):limit(n) {}
    void add(size_t n) {
        std::lock_guard<std::mutex> guard(mutex);
        if(n>limit-used) throw std::runtime_error("native frame allocation exceeds max_in_flight_bytes (requested="+std::to_string(n)+", used="+std::to_string(used)+", limit="+std::to_string(limit)+")");
        used+=n; peak=std::max(peak,used);
    }
    void sub(size_t n) { std::lock_guard<std::mutex> guard(mutex); used-=n; }
};
using BudgetRef=std::shared_ptr<Budget>;
struct Charge {
    BudgetRef budget; size_t size=0;
    explicit Charge(BudgetRef b):budget(std::move(b)) {}
    Charge(const Charge&)=delete;
    ~Charge(){if(budget)budget->sub(size);}
    void add(size_t n){budget->add(n);size+=n;}
    void sub(size_t n){budget->sub(n);size-=n;}
};
size_t checked_area(int w,int h,size_t item=1) {
    if(w<0||h<0||w>1000000||h>1000000)throw std::runtime_error("invalid or excessive raster dimensions");
    const uint64_t n=uint64_t(w)*uint64_t(h)*item;
    if(n>std::numeric_limits<size_t>::max())throw std::runtime_error("raster allocation overflow");
    return size_t(n);
}
int coordinate(int64_t v) {
    if(v < -4000000 || v > 4000000)throw std::runtime_error("image coordinate outside supported safe range");
    return int(v);
}
struct Plane {int x,y,w,h,type;uint32_t color;std::vector<uint8_t> pixels;};
struct Images {
    Charge charge; std::vector<Plane> planes; int changed=0;
    explicit Images(BudgetRef b):charge(std::move(b)){}
};
// Keep this private ABI declaration independent from patched headers: the
// bridge must compile/load with an ordinary system libass as well.
struct EventImageView {
    int type, dst_x, dst_y, w, h, stride;
    uint32_t color;
    const uint8_t* bitmap;
};
using EventCallback=void(*)(void*,const ASS_Track*,int,const EventImageView*,size_t);
struct EventExtension {
    unsigned abi=0;
    void (*set)(ASS_Renderer*,EventCallback,void*)=nullptr;
    EventExtension() {
        Dl_info info{};
        if(!dladdr(reinterpret_cast<void*>(&ass_library_version),&info)||!info.dli_fname)return;
        void* handle=dlopen(info.dli_fname,RTLD_NOW|RTLD_LOCAL);
        if(!handle)return;
        auto version=reinterpret_cast<unsigned(*)()>(dlsym(handle,"assglass_event_export_abi_version"));
        set=reinterpret_cast<decltype(set)>(dlsym(handle,"assglass_set_event_images_callback"));
        if(version&&set)abi=version();
        if(abi!=1)set=nullptr;
        // Retain the exact library handle for the process lifetime.
    }
};
EventExtension& event_extension(){static EventExtension extension;return extension;}
struct EventFrame {
    Charge charge;std::vector<int> indices;std::vector<std::shared_ptr<Images>> images;
    int changed=0;
    EventFrame(BudgetRef b,const std::vector<int>& selected):charge(b) {
        charge.add(selected.size()*(sizeof(int)+sizeof(std::shared_ptr<Images>)));
        indices=selected;images.resize(selected.size());
    }
};
struct Mask {
    Charge charge; int x=0,y=0,w=0,h=0; std::vector<float> pixels;
    explicit Mask(BudgetRef b):charge(std::move(b)){}
    void allocate(int ax,int ay,int aw,int ah){
        x=ax;y=ay;w=aw;h=ah;size_t bytes=checked_area(w,h,sizeof(float));charge.add(bytes);pixels.assign(bytes/sizeof(float),0);
    }
};
struct Weights {Charge charge;std::vector<uint8_t> pixels;explicit Weights(BudgetRef b):charge(std::move(b)){};};
template<class T> using Ref=std::shared_ptr<T>;
template<class T> Ref<T>& unwrap(void* p){if(!p)throw std::runtime_error("native handle is closed");return *static_cast<Ref<T>*>(p);}
BudgetRef get_budget(void* p){return unwrap<Budget>(p);}
void copy_plane(Images& out,int x,int y,int w,int h,int stride,uint32_t color,int type,const uint8_t* data,size_t length){
    if(w==0||h==0)return;
    checked_area(w,h);coordinate(x);coordinate(y);coordinate(int64_t(x)+w);coordinate(int64_t(y)+h);
    if(stride<w || !data)throw std::runtime_error("invalid image stride or bitmap");
    uint64_t required=uint64_t(h-1)*uint64_t(stride)+uint64_t(w);
    if(required>length)throw std::runtime_error("bitmap buffer is shorter than its final row");
    if((color&255)==255)return;
    bool ink=false;
    for(int row=0;row<h&&!ink;row++)for(int col=0;col<w;col++)if(data[size_t(row)*stride+col]){ink=true;break;}
    if(!ink)return;
    size_t bytes=checked_area(w,h);
    if(out.planes.size()==out.planes.capacity()){
        size_t old_capacity=out.planes.capacity();
        size_t new_capacity=std::max(size_t(1),old_capacity*2);
        out.charge.add(new_capacity*sizeof(Plane));
        out.planes.reserve(new_capacity);
        out.charge.sub(old_capacity*sizeof(Plane));
    }
    out.charge.add(bytes);
    Plane im{x,y,w,h,type,color,{}};im.pixels.resize(bytes);
    // libass does not promise stride bytes after the final row.
    for(int row=0;row<h;row++)std::memcpy(im.pixels.data()+size_t(row)*w,data+size_t(row)*stride,w);
    out.planes.push_back(std::move(im));
}
struct Session {
    BudgetRef budget; ASS_Library* library=nullptr;ASS_Renderer* renderer=nullptr;ASS_Track* track=nullptr;
    std::string logs; std::mutex mutex;
    bool event_mode=false;std::vector<int> selected;std::unique_ptr<Charge> selection_charge;
    EventFrame* collector=nullptr;std::exception_ptr callback_error;
    explicit Session(BudgetRef b):budget(std::move(b)){}
    ~Session(){if(track)ass_free_track(track);if(renderer)ass_renderer_done(renderer);if(library)ass_library_done(library);}
};
void event_callback(void* opaque,const ASS_Track* track,int index,const EventImageView* images,size_t count) noexcept {
    auto& s=*static_cast<Session*>(opaque);
    if(s.callback_error)return;
    try {
        if(!s.collector||track!=s.track)throw std::runtime_error("EventImages callback track/collector mismatch");
        if(index<0||index>=track->n_events||count==std::numeric_limits<size_t>::max())
            throw std::runtime_error("libass EventImages descriptor allocation or identity failure");
        auto it=std::lower_bound(s.selected.begin(),s.selected.end(),index);
        if(it==s.selected.end()||*it!=index)return;
        const size_t slot=size_t(it-s.selected.begin());
        if(s.collector->images[slot])throw std::runtime_error("duplicate EventImages callback index");
        auto result=std::make_shared<Images>(s.budget);
        for(size_t i=0;i<count;i++) {
            const auto& im=images[i];
            copy_plane(*result,im.dst_x,im.dst_y,im.w,im.h,im.stride,im.color,im.type,im.bitmap,std::numeric_limits<size_t>::max());
        }
        s.collector->images[slot]=std::move(result);
    } catch(...) {
        // Store the exception without allocation. Never unwind through libass;
        // the standard render finishes before the outer C ABI reports failure.
        s.callback_error=std::current_exception();
    }
}
void log_callback(int level,const char* fmt,va_list args,void* data){
    if(level>6)return;auto* s=static_cast<Session*>(data);char text[2048];vsnprintf(text,sizeof(text),fmt,args);
    if(s->logs.size()<65536){s->logs+=text;s->logs+='\n';}
}
struct Stats {int x0=0,y0=0,x1=0,y1=0;uint32_t product=0;bool nonempty=false;};
Stats stats(const Images& ims,int types,double opacity_threshold){
    if(!std::isfinite(opacity_threshold)||opacity_threshold<0||opacity_threshold>1)throw std::runtime_error("opacity_threshold must be finite in [0,1]");
    Stats s;
    for(const auto& im:ims.planes){
        if(im.type<0||im.type>2||!(types&(1<<im.type)))continue;
        for(int y=0;y<im.h;y++)for(int x=0;x<im.w;x++){
            auto c=im.pixels[size_t(y)*im.w+x];if(!c)continue;
            const uint32_t product=uint32_t(c)*(255-(im.color&255));
            s.product=std::max(s.product,product);
            // Compare each original image's effective opacity without an 8-bit
            // intermediate. Any qualifying image contributes to the max union;
            // faint overlapping images do not accumulate into solid ink.
            if(double(product)/65025.0<=opacity_threshold)continue;
            int px=im.x+x,py=im.y+y;
            if(!s.nonempty){s.x0=px;s.x1=px+1;s.y0=py;s.y1=py+1;s.nonempty=true;}
            else{s.x0=std::min(s.x0,px);s.y0=std::min(s.y0,py);s.x1=std::max(s.x1,px+1);s.y1=std::max(s.y1,py+1);}
        }
    }
    return s;
}
float ss4(int i,int j,int x0,int y0,int x1,int y1,int requested_radius){
    const int64_t L=int64_t(x0)*8-4,R=int64_t(x1)*8-4,T=int64_t(y0)*8-4,B=int64_t(y1)*8-4;
    const int64_t r=std::min({int64_t(requested_radius)*8,(R-L)/2,(B-T)/2});
    const int offsets[4]={-3,-1,1,3};int count=0;
    for(int oy:offsets)for(int ox:offsets){
        int64_t x=int64_t(i)*8+ox,y=int64_t(j)*8+oy;
        if(x<L||x>=R||y<T||y>=B)continue;
        int64_t dx=std::max({L+r-x,int64_t(0),x-(R-r)}),dy=std::max({T+r-y,int64_t(0),y-(B-r)});
        if(dx*dx+dy*dy<=r*r)++count;
    }
    return float(count)/16.0f;
}
uint8_t quantize(float v){return uint8_t(std::floor(std::max(0.0f,std::min(1.0f,v))*255.0f+0.5f));}
float sample(const Mask& m,int x,int y){if(x<m.x||y<m.y||x>=m.x+m.w||y>=m.y+m.h)return 0;return m.pixels[size_t(y-m.y)*m.w+(x-m.x)];}
#define BEGIN try { error.clear();
#define END_NULL } catch(const std::exception& e){error=e.what();return nullptr;}
#define END_INT } catch(const std::exception& e){error=e.what();return -1;}
}
extern "C" {
const char* ag_error(){return error.c_str();}
void* ag_budget_new(size_t limit){BEGIN return new BudgetRef(std::make_shared<Budget>(limit));END_NULL}
void ag_budget_free(void* p){delete static_cast<BudgetRef*>(p);}
size_t ag_budget_used(void* p){return p?unwrap<Budget>(p)->used:0;}
size_t ag_budget_peak(void* p){return p?unwrap<Budget>(p)->peak:0;}
int ag_libass_version(){return ass_library_version();}
unsigned ag_event_export_abi(){return event_extension().abi;}
const char* ag_libass_path(){static std::string path=[](){Dl_info info{};if(dladdr(reinterpret_cast<void*>(&ass_library_version),&info)&&info.dli_fname)return std::string(info.dli_fname);return std::string("unknown");}();return path.c_str();}
int64_t ag_time_ms(int64_t pts,int64_t num,int64_t den){volatile double tb=double(num)/double(den);volatile double seconds=double(pts)*tb;volatile double millis=seconds*1000.0;return int64_t(millis);}
void* ag_session_new(const char* data,size_t size,int w,int h,const char* fonts,void* budget){BEGIN
    checked_area(w,h);if(!w||!h)throw std::runtime_error("render frame must not be empty");
    auto s=std::make_unique<Session>(get_budget(budget));
    s->library=ass_library_init();if(!s->library)throw std::runtime_error("ass_library_init failed");
    ass_set_message_cb(s->library,log_callback,s.get());
    if(fonts&&*fonts)ass_set_fonts_dir(s->library,fonts);
    ass_set_extract_fonts(s->library,1);
    s->renderer=ass_renderer_init(s->library);if(!s->renderer)throw std::runtime_error("ass_renderer_init failed");
    ass_set_fonts(s->renderer,nullptr,nullptr,1,nullptr,1);
    std::vector<char> input(data,data+size);input.push_back(0);
    s->track=ass_read_memory(s->library,input.data(),size,nullptr);
    if(!s->track)throw std::runtime_error("libass could not parse the ASS data");
    ass_set_frame_size(s->renderer,w,h);ass_set_storage_size(s->renderer,w,h);
    return s.release();END_NULL}
void ag_session_free(void* p){delete static_cast<Session*>(p);}
const char* ag_session_logs(void* p){return static_cast<Session*>(p)->logs.c_str();}
void* ag_session_render(void* p,int64_t ms){BEGIN
    if(!p)throw std::runtime_error("render session is closed");auto& s=*static_cast<Session*>(p);std::lock_guard<std::mutex> lock(s.mutex);
    auto result=std::make_shared<Images>(s.budget);ASS_Image* im=ass_render_frame(s.renderer,s.track,ms,&result->changed);
    for(;im;im=im->next)copy_plane(*result,im->dst_x,im->dst_y,im->w,im->h,im->stride,im->color,int(im->type),im->bitmap,std::numeric_limits<size_t>::max());
    return new Ref<Images>(std::move(result));END_NULL}
int ag_session_event_count(void* p){return static_cast<Session*>(p)->track->n_events;}
int ag_session_event_metadata(void* p,int index,int64_t* timing,int* fields,const char** strings){BEGIN
    auto& s=*static_cast<Session*>(p);
    if(index<0||index>=s.track->n_events)throw std::runtime_error("invalid event metadata index");
    const auto& event=s.track->events[index];
    timing[0]=event.Start;timing[1]=event.Duration;
    fields[0]=event.Layer;fields[1]=event.MarginL;fields[2]=event.MarginR;fields[3]=event.MarginV;
    strings[0]=event.Style>=0&&event.Style<s.track->n_styles?s.track->styles[event.Style].Name:"";
    strings[1]=event.Name;strings[2]=event.Text;strings[3]=event.Effect;
    return 0;END_INT}
int ag_session_enable_events(void* p,const int* indices,size_t count){BEGIN
    auto& s=*static_cast<Session*>(p);std::lock_guard<std::mutex> lock(s.mutex);
    if(!event_extension().set)throw std::runtime_error("EventImages requires libass extension ABI 1; run scripts/build_libass.sh and rebuild FFmpeg/native bridge");
    if(s.event_mode)throw std::runtime_error("EventImages selection is immutable");
    if(count>size_t(s.track->n_events))throw std::runtime_error("too many selected event indices");
    s.selection_charge=std::make_unique<Charge>(s.budget);s.selection_charge->add(count*sizeof(int));
    s.selected.assign(indices,indices+count);std::sort(s.selected.begin(),s.selected.end());
    for(size_t i=0;i<count;i++)if(s.selected[i]<0||s.selected[i]>=s.track->n_events||(i&&s.selected[i]==s.selected[i-1]))
        throw std::runtime_error("selected event indices must be distinct valid track indices");
    event_extension().set(s.renderer,event_callback,&s);s.event_mode=true;return 0;END_INT}
void* ag_session_render_events(void* p,int64_t ms){BEGIN
    if(!p)throw std::runtime_error("render session is closed");auto& s=*static_cast<Session*>(p);std::lock_guard<std::mutex> lock(s.mutex);
    if(!s.event_mode)throw std::runtime_error("EventImages callback is not enabled");
    auto result=std::make_unique<EventFrame>(s.budget,s.selected);
    s.callback_error=nullptr;s.collector=result.get();
    ass_render_frame(s.renderer,s.track,ms,&result->changed);
    s.collector=nullptr;
    if(s.callback_error)std::rethrow_exception(s.callback_error);
    return result.release();END_NULL}
void ag_event_frame_free(void* p){delete static_cast<EventFrame*>(p);}
int ag_event_frame_changed(void* p){return static_cast<EventFrame*>(p)->changed;}
void* ag_event_frame_take(void* p,int index,void* budget){BEGIN
    if(!p)throw std::runtime_error("EventImages frame is released");auto& frame=*static_cast<EventFrame*>(p);
    auto it=std::lower_bound(frame.indices.begin(),frame.indices.end(),index);
    if(it==frame.indices.end()||*it!=index)throw std::runtime_error("event index was not selected");
    auto& image=frame.images[size_t(it-frame.indices.begin())];
    auto result=image?std::move(image):std::make_shared<Images>(get_budget(budget));
    result->changed=frame.changed;
    return new Ref<Images>(std::move(result));END_NULL}
void* ag_images_new(void* budget){BEGIN return new Ref<Images>(std::make_shared<Images>(get_budget(budget)));END_NULL}
void* ag_images_combine(void** images,size_t count,void* budget){BEGIN
    auto result=std::make_shared<Images>(get_budget(budget));
    for(size_t i=0;i<count;i++) {
        const auto& source=*unwrap<Images>(images[i]);result->changed=std::max(result->changed,source.changed);
        for(const auto& im:source.planes)copy_plane(*result,im.x,im.y,im.w,im.h,im.w,im.color,im.type,im.pixels.data(),im.pixels.size());
    }
    return new Ref<Images>(std::move(result));END_NULL}
int ag_images_append(void* p,int x,int y,int w,int h,int stride,uint32_t color,int type,const uint8_t* data,size_t length){BEGIN copy_plane(*unwrap<Images>(p),x,y,w,h,stride,color,type,data,length);return 0;END_INT}
void* ag_images_retain(void* p){BEGIN return new Ref<Images>(unwrap<Images>(p));END_NULL}
void ag_images_free(void* p){delete static_cast<Ref<Images>*>(p);}
size_t ag_images_count(void* p){return unwrap<Images>(p)->planes.size();}
int ag_images_changed(void* p){return unwrap<Images>(p)->changed;}
int ag_images_get(void* p,size_t index,int* desc,uint32_t* color,const uint8_t** data){BEGIN
    auto& im=unwrap<Images>(p)->planes.at(index);desc[0]=im.x;desc[1]=im.y;desc[2]=im.w;desc[3]=im.h;desc[4]=im.type;*color=im.color;*data=im.pixels.data();return 0;END_INT}
uint64_t ag_images_digest(void* p){
    uint64_t hash=14695981039346656037ULL;
    auto add=[&](uint8_t b){hash=(hash^b)*1099511628211ULL;};
    for(const auto& im:unwrap<Images>(p)->planes){for(int64_t n:{int64_t(im.x),int64_t(im.y),int64_t(im.w),int64_t(im.h),int64_t(im.type),int64_t(im.color)})for(int s=0;s<8;s++)add(uint8_t(uint64_t(n)>>(8*s)));for(auto v:im.pixels)add(v);}return hash;
}
int ag_images_stats(void* p,int types,double opacity_threshold,int* bbox,uint32_t* peak){BEGIN auto s=stats(*unwrap<Images>(p),types,opacity_threshold);bbox[0]=s.x0;bbox[1]=s.y0;bbox[2]=s.x1;bbox[3]=s.y1;*peak=s.product;return s.nonempty?1:0;END_INT}
void* ag_mask_new(int x,int y,int w,int h,const float* values,void* budget){BEGIN
    coordinate(x);coordinate(y);coordinate(int64_t(x)+w);coordinate(int64_t(y)+h);
    auto result=std::make_shared<Mask>(get_budget(budget));result->allocate(x,y,w,h);
    for(size_t i=0;i<result->pixels.size();i++){if(!std::isfinite(values[i])||values[i]<0||values[i]>1)throw std::runtime_error("mask values must be finite in [0,1]");result->pixels[i]=values[i];}
    return new Ref<Mask>(std::move(result));END_NULL}
void* ag_box(void* images,int fw,int fh,int types,int px,int py,int radius,float sigma,float strength,double opacity_threshold,int visual,void* budget){BEGIN
    checked_area(fw,fh);if(px<0||py<0||radius<0||px>1000000||py>1000000||radius>1000000||!std::isfinite(sigma)||sigma<0||sigma>100000||!std::isfinite(strength)||strength<0||strength>1)throw std::runtime_error("invalid box configuration");
    auto b=get_budget(budget);auto result=std::make_shared<Mask>(b);auto s=stats(*unwrap<Images>(images),types,opacity_threshold);
    if(!s.nonempty||strength==0)return new Ref<Mask>(result);
    int x0=coordinate(int64_t(s.x0)-px),x1=coordinate(int64_t(s.x1)+px),y0=coordinate(int64_t(s.y0)-py),y1=coordinate(int64_t(s.y1)+py);
    int halo=int(std::ceil(3.0*double(sigma)));
    int rx0=coordinate(int64_t(x0)-halo),ry0=coordinate(int64_t(y0)-halo),rx1=coordinate(int64_t(x1)+halo),ry1=coordinate(int64_t(y1)+halo);
    int ox0=std::max(0,rx0),oy0=std::max(0,ry0),ox1=std::min(fw,rx1),oy1=std::min(fh,ry1);
    if(ox0>=ox1||oy0>=oy1)return new Ref<Mask>(result);
    int ww=rx1-rx0,wh=ry1-ry0;size_t bytes=checked_area(ww,wh,sizeof(float));Charge work(b);work.add(bytes);
    std::vector<float> source(bytes/sizeof(float),0);
    for(int y=y0;y<y1;y++)for(int x=x0;x<x1;x++)source[size_t(y-ry0)*ww+(x-rx0)]=ss4(x,y,x0,y0,x1,y1,radius);
    if(halo){
        Charge filter_work(b);filter_work.add(bytes+size_t(2*halo+1)*sizeof(float));std::vector<float> temp(source.size(),0),kernel(2*halo+1);
        double total=0;for(int i=-halo;i<=halo;i++)total+=std::exp(-double(i)*i/(2.0*double(sigma)*sigma));
        for(int i=-halo;i<=halo;i++)kernel[i+halo]=float(std::exp(-double(i)*i/(2.0*double(sigma)*sigma))/total);
        for(int y=0;y<wh;y++)for(int x=0;x<ww;x++){float v=0;for(int k=-halo;k<=halo;k++)if(x+k>=0&&x+k<ww)v+=source[size_t(y)*ww+x+k]*kernel[k+halo];temp[size_t(y)*ww+x]=v;}
        for(int y=0;y<wh;y++)for(int x=0;x<ww;x++){float v=0;for(int k=-halo;k<=halo;k++)if(y+k>=0&&y+k<wh)v+=temp[size_t(y+k)*ww+x]*kernel[k+halo];source[size_t(y)*ww+x]=v;}
    }
    result->allocate(ox0,oy0,ox1-ox0,oy1-oy0);float gain=visual?float(s.product)/65025.0f:1.0f;
    for(int y=oy0;y<oy1;y++)for(int x=ox0;x<ox1;x++)result->pixels[size_t(y-oy0)*result->w+x-ox0]=std::max(0.0f,std::min(1.0f,(source[size_t(y-ry0)*ww+x-rx0]*gain)*strength));
    return new Ref<Mask>(std::move(result));END_NULL}
void* ag_mask_union(void** masks,size_t count,int fw,int fh,void* budget){BEGIN
    checked_area(fw,fh);int x0=fw,y0=fh,x1=0,y1=0;
    for(size_t i=0;i<count;i++){auto& m=*unwrap<Mask>(masks[i]);if(!m.w||!m.h)continue;x0=std::min(x0,std::max(0,m.x));y0=std::min(y0,std::max(0,m.y));x1=std::max(x1,std::min(fw,m.x+m.w));y1=std::max(y1,std::min(fh,m.y+m.h));}
    auto result=std::make_shared<Mask>(get_budget(budget));
    if(x0>=x1||y0>=y1)return new Ref<Mask>(result);
    result->allocate(x0,y0,x1-x0,y1-y0);
    for(size_t i=0;i<count;i++){auto& m=*unwrap<Mask>(masks[i]);for(int y=std::max(y0,m.y);y<std::min(y1,m.y+m.h);y++)for(int x=std::max(x0,m.x);x<std::min(x1,m.x+m.w);x++){auto& p=result->pixels[size_t(y-y0)*result->w+x-x0];p=std::max(p,sample(m,x,y));}}
    return new Ref<Mask>(std::move(result));END_NULL}
void* ag_mask_retain(void* p){BEGIN return new Ref<Mask>(unwrap<Mask>(p));END_NULL}
void ag_mask_free(void* p){delete static_cast<Ref<Mask>*>(p);}
int ag_mask_get(void* p,int* roi,const float** data){BEGIN auto& m=*unwrap<Mask>(p);roi[0]=m.x;roi[1]=m.y;roi[2]=m.x+m.w;roi[3]=m.y+m.h;*data=m.pixels.data();return 0;END_INT}
void* ag_weights(void* mask,int fw,int fh,void* budget){BEGIN
    size_t ysize=checked_area(fw,fh);if(!fw||!fh||(fw%2)||(fh%2))throw std::runtime_error("yuv420p weights require positive even dimensions");
    auto& m=*unwrap<Mask>(mask);auto out=std::make_shared<Weights>(get_budget(budget));size_t uvsize=ysize/4;out->charge.add(ysize+2*uvsize);out->pixels.resize(ysize+2*uvsize);
    for(int y=0;y<fh;y++)for(int x=0;x<fw;x++)out->pixels[size_t(y)*fw+x]=quantize(sample(m,x,y));
    // left phase: chroma centers (2u, 2v+.5); separable tent radius 2.
    const float wx[3]={.25f,.5f,.25f},wy[4]={.125f,.375f,.375f,.125f};
    for(int v=0;v<fh/2;v++)for(int u=0;u<fw/2;u++){
        float sum=0;
        for(int j=0;j<4;j++){float row=0;int y=std::max(0,std::min(fh-1,2*v+j-1));for(int i=0;i<3;i++){int x=std::max(0,std::min(fw-1,2*u+i-1));row+=sample(m,x,y)*wx[i];}sum+=row*wy[j];}
        auto q=quantize(sum);size_t index=size_t(v)*(fw/2)+u;out->pixels[ysize+index]=q;out->pixels[ysize+uvsize+index]=q;
    }
    return new Ref<Weights>(std::move(out));END_NULL}
void* ag_weights_retain(void* p){BEGIN return new Ref<Weights>(unwrap<Weights>(p));END_NULL}
void ag_weights_free(void* p){delete static_cast<Ref<Weights>*>(p);}
size_t ag_weights_get(void* p,const uint8_t** data){auto& w=*unwrap<Weights>(p);*data=w.pixels.data();return w.pixels.size();}
}
