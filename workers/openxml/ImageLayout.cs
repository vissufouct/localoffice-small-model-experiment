using System.Globalization;
using System.Text.Json;
using DocumentFormat.OpenXml;
using W=DocumentFormat.OpenXml.Wordprocessing;
using DW=DocumentFormat.OpenXml.Drawing.Wordprocessing;

// Package writing, stale checks and exact Undo remain with the registered batch/ledger.
static class ImageLayout
{
    const string Ns="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing";
    static readonly string[] Fields={"mode","wrap","horizontal_origin","vertical_origin","horizontal_offset_inches","vertical_offset_inches"};
    static string Attr(OpenXmlElement e,string name)=>e.GetAttributes().FirstOrDefault(a=>a.LocalName==name && a.NamespaceUri=="").Value ?? "";
    static void Set(OpenXmlElement e,string name,string value) { if(Attr(e,name)!=value)e.SetAttribute(new OpenXmlAttribute(name,"",value)); }
    static OpenXmlElement Child(OpenXmlElement e,string name)=>e.ChildElements.Single(c=>c.LocalName==name && c.NamespaceUri==Ns);
    static string Wrap(OpenXmlElement e)=>e.ChildElements.Single(c=>c.NamespaceUri==Ns && c.LocalName.StartsWith("wrap",StringComparison.Ordinal)).LocalName;
    static void Require(bool condition,string message) { if(!condition)throw new InvalidDataException(message); }
    static void Check(OpenXmlElement frame)
    {
        var common=new[]{"extent","effectExtent","docPr","cNvGraphicFramePr"};
        var extra=new[]{"simplePos","positionH","positionV","wrapSquare","wrapTopAndBottom","wrapNone"};
        Require(frame.ChildElements.All(c=>(c.NamespaceUri==Ns && (common.Contains(c.LocalName) || (frame is DW.Anchor && extra.Contains(c.LocalName)))) || (c.LocalName=="graphic" && c.NamespaceUri=="http://schemas.openxmlformats.org/drawingml/2006/main")),"Unsupported image layout children; relative sizing, tight wrapping and extensions are read-only");
        Require(frame.ChildElements.GroupBy(c=>c.NamespaceUri+":"+c.LocalName).All(g=>g.Count()==1),"Duplicate image layout children are unsupported");
        if(frame is not DW.Anchor)return;
        Require(frame.ChildElements.Count(c=>c.NamespaceUri==Ns && c.LocalName.StartsWith("wrap",StringComparison.Ordinal))==1,"Image layout requires one supported wrap element");
        Require(Attr(frame,"simplePos") is "0" or "false","Simple-position anchors are unsupported");
        foreach(var name in new[]{"simplePos","positionH","positionV"})Require(frame.ChildElements.Any(c=>c.LocalName==name && c.NamespaceUri==Ns),"Incomplete floating image layout");
        foreach(var axis in new[]{"H","V"}) {
            var p=Child(frame,"position"+axis);
            Require(p.ChildElements.Count==1 && p.FirstChild is DW.PositionOffset,"Aligned or percentage image positioning is unsupported");
            Require((axis=="H"?new[]{"page","margin","column"}:new[]{"page","margin","paragraph"}).Contains(Attr(p,"relativeFrom")),"Unsupported image position origin");
            Require(long.TryParse(p.FirstChild!.InnerText,NumberStyles.Integer,CultureInfo.InvariantCulture,out _),"Invalid image position offset");
        }
    }
    static long Offset(JsonElement value)
    {
        Require(value.ValueKind==JsonValueKind.Number && value.TryGetDecimal(out _),"Image offsets must be numeric inches");
        decimal inches=value.GetDecimal();Require(inches>=-22 && inches<=22,"Image offsets must be within -22 to 22 inches");
        return (long)decimal.Round(inches*914400m,0,MidpointRounding.AwayFromZero);
    }
    static void CheckPicture(W.Drawing drawing)
    {
        var parts=ImageFormatting.Parts(drawing);
        Require(parts.picture.Parent!.ChildElements.Count==1, "Composite graphic content is unsupported");
    }
    internal static object Describe(W.Drawing drawing)
    {
        try {
            CheckPicture(drawing);var f=drawing.FirstChild!;Check(f);
            if(f is DW.Inline)return new { supported=true,mode="inline" };
            return new { supported=true,mode="floating",wrap=Wrap(f) switch {"wrapSquare"=>"square","wrapTopAndBottom"=>"top_bottom",_=>"none"},
                horizontal_origin=Attr(Child(f,"positionH"),"relativeFrom"),vertical_origin=Attr(Child(f,"positionV"),"relativeFrom"),
                horizontal_offset_inches=decimal.Parse(Child(f,"positionH").FirstChild!.InnerText,CultureInfo.InvariantCulture)/914400m,
                vertical_offset_inches=decimal.Parse(Child(f,"positionV").FirstChild!.InnerText,CultureInfo.InvariantCulture)/914400m };
        } catch(InvalidDataException error) { return new { supported=false,reason=error.Message }; }
    }
    internal static void Apply(W.Drawing drawing,JsonElement properties)
    {
        CheckPicture(drawing);
        var original=drawing.FirstChild!;Check(original);
        Require(properties.ValueKind==JsonValueKind.Object,"Image layout properties must be an object");
        var supplied=properties.EnumerateObject().ToList();
        Require(supplied.Count>0 && supplied.All(p=>Fields.Contains(p.Name) && p.Value.ValueKind!=JsonValueKind.Null) && supplied.Select(p=>p.Name).Distinct().Count()==supplied.Count,"Unknown, null, duplicate or empty image layout properties");
        bool Has(string name)=>properties.TryGetProperty(name,out _);
        string Text(string name) { Require(properties.GetProperty(name).ValueKind==JsonValueKind.String,"Image layout options must be strings");return properties.GetProperty(name).GetString()!; }
        var mode=Has("mode")?Text("mode"):(original is DW.Anchor?"floating":"inline");
        Require(mode is "inline" or "floating","Image mode must be inline or floating");
        if(Has("wrap"))Require(new[]{"square","top_bottom","none"}.Contains(Text("wrap")),"Unsupported image wrap");
        if(Has("horizontal_origin"))Require(new[]{"page","margin","column"}.Contains(Text("horizontal_origin")),"Unsupported horizontal origin");
        if(Has("vertical_origin"))Require(new[]{"page","margin","paragraph"}.Contains(Text("vertical_origin")),"Unsupported vertical origin");
        foreach(var name in new[]{"horizontal_offset_inches","vertical_offset_inches"})if(Has(name))Offset(properties.GetProperty(name));
        if(mode=="inline")Require(supplied.Count==1 && Has("mode"),"Inline mode accepts only mode; conversion explicitly removes floating placement and wrapping");
        bool converting=(mode=="inline")!=(original is DW.Inline);
        if(converting && mode=="floating")Require(Fields.All(Has),"Inline to floating conversion requires mode, wrap, both origins and both offsets explicitly");
        OpenXmlCompositeElement frame;
        if(converting) {
            if(original is DW.Anchor) {
                Require(Attr(original,"hidden") is "" or "0" or "false", "Hidden floating images cannot be converted inline without changing visibility");
                var wrap=Child(original,Wrap(original));
                Require(!wrap.HasChildren && wrap.GetAttributes().All(a=>a.NamespaceUri=="" && a.LocalName is "wrapText" or "distT" or "distB" or "distL" or "distR"),"Unsupported wrap extensions cannot be converted safely");
            }
            // Refuse attributes that cannot be retained by the target schema.
            var known=new[]{"distT","distB","distL","distR","simplePos","relativeHeight","behindDoc","locked","layoutInCell","hidden","allowOverlap"};
            Require(original.GetAttributes().All(a=>(a.NamespaceUri=="" && known.Contains(a.LocalName)) || (a.NamespaceUri=="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing" && a.LocalName is "anchorId" or "editId")),"Unsupported image layout attributes cannot be converted safely");
            frame=mode=="inline"?new DW.Inline():new DW.Anchor();
            foreach(var ns in original.NamespaceDeclarations)frame.AddNamespaceDeclaration(ns.Key,ns.Value);
            foreach(var a in original.GetAttributes())if(a.LocalName is "distT" or "distB" or "distL" or "distR" || a.NamespaceUri!="")frame.SetAttribute(a);
            foreach(var c in original.ChildElements)if(c.LocalName is "extent" or "effectExtent" or "docPr" or "cNvGraphicFramePr" or "graphic")frame.Append(c.CloneNode(true));
            if(mode=="floating") {
                foreach(var pair in new[]{("simplePos","0"),("relativeHeight","0"),("behindDoc","0"),("locked","0"),("layoutInCell","1"),("allowOverlap","1")})Set(frame,pair.Item1,pair.Item2);
                frame.AddChild(new DW.SimplePosition { X=0L,Y=0L });
                frame.AddChild(new DW.HorizontalPosition(new DW.PositionOffset("0")));
                frame.AddChild(new DW.VerticalPosition(new DW.PositionOffset("0")));
            }
        } else frame=(OpenXmlCompositeElement)original.CloneNode(true);
        if(mode=="floating") {
            foreach(var (axis,prefix) in new[]{("H","horizontal"),("V","vertical")}) {
                var position=Child(frame,"position"+axis);
                if(Has(prefix+"_origin"))Set(position,"relativeFrom",Text(prefix+"_origin"));
                if(Has(prefix+"_offset_inches")) {
                    var value=Offset(properties.GetProperty(prefix+"_offset_inches"));
                    if(!long.TryParse(position.FirstChild!.InnerText,out var old) || old!=value)((DW.PositionOffset)position.FirstChild).Text=value.ToString(CultureInfo.InvariantCulture);
                }
            }
            if(Has("wrap")) {
                string name=Text("wrap") switch {"square"=>"wrapSquare","top_bottom"=>"wrapTopAndBottom",_=>"wrapNone"};
                var old=frame.ChildElements.FirstOrDefault(c=>c.NamespaceUri==Ns && c.LocalName.StartsWith("wrap",StringComparison.Ordinal));
                if(old?.LocalName!=name) {
                    Require(old==null || (!old.HasChildren && old.GetAttributes().All(a=>a.NamespaceUri=="" && a.LocalName is "wrapText" or "distT" or "distB" or "distL" or "distR")),"Unsupported wrap extensions cannot be discarded");
                    // Per-wrap distances would be lost on a change of wrapping semantics.
                    Require(old==null || !old.GetAttributes().Any(a=>a.LocalName.StartsWith("dist",StringComparison.Ordinal)),"Per-wrap distances require explicit support before changing wrap mode");
                    old?.Remove();OpenXmlElement wrap=name switch {"wrapSquare"=>new DW.WrapSquare(),"wrapTopAndBottom"=>new DW.WrapTopBottom(),_=>new DW.WrapNone()};
                    if(name=="wrapSquare")Set(wrap,"wrapText","bothSides");frame.AddChild(wrap);
                }
            }
        }
        if(frame.OuterXml!=original.OuterXml)drawing.ReplaceChild(frame,original);
    }
}
