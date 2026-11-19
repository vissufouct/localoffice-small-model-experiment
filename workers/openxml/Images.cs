using System.Text;
using System.Text.Json;
using System.Xml.Linq;
using DocumentFormat.OpenXml;
using W=DocumentFormat.OpenXml.Wordprocessing;

static class Images
{
    static readonly XNamespace WP="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing";
    static readonly XNamespace A="http://schemas.openxmlformats.org/drawingml/2006/main";
    static readonly XNamespace PIC="http://schemas.openxmlformats.org/drawingml/2006/picture";
    static readonly XNamespace R="http://schemas.openxmlformats.org/officeDocument/2006/relationships";
    static readonly XNamespace REL="http://schemas.openxmlformats.org/package/2006/relationships";
    static readonly XNamespace CT="http://schemas.openxmlformats.org/package/2006/content-types";
    static readonly XNamespace WN="http://schemas.openxmlformats.org/wordprocessingml/2006/main";
    static XDocument Xml(byte[] bytes) { using var stream=new MemoryStream(bytes);return XDocument.Load(stream); }
    static XAttribute Attr(string name,object value)=>new(name,value);
    internal static string Relationships(string mainName) {
        var slash=mainName.LastIndexOf('/');return (slash<0?"":mainName[..(slash+1)])+"_rels/"+mainName[(slash+1)..]+".rels";
    }
    static string Embed(Dictionary<string,byte[]> parts,string mainName,JsonElement asset)
    {
        var data=File.ReadAllBytes(asset.GetProperty("input").GetString()!);
        if(data.Length>50_000_000 || Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(data)).ToLowerInvariant()!=asset.GetProperty("hash").GetString())
            throw new InvalidDataException("Image snapshot hash/size mismatch");
        var media=asset.GetProperty("media").GetString()!;
        if(parts.TryGetValue(media,out var existing) && !existing.SequenceEqual(data))throw new InvalidDataException("Image part collision");
        var relName=Relationships(mainName);
        var rels=parts.ContainsKey(relName)?Xml(parts[relName]):new XDocument(new XElement(REL+"Relationships"));
        int n=1;while(rels.Root!.Elements().Any(e=>(string?)e.Attribute("Id")=="rIdLocalOffice"+n))n++;
        var rid="rIdLocalOffice"+n;
        rels.Root!.Add(new XElement(REL+"Relationship",Attr("Id",rid),Attr("Type",R.NamespaceName+"/image"),Attr("Target","/"+media)));
        var types=Xml(parts["[Content_Types].xml"]);
        if(!types.Root!.Elements(CT+"Override").Any(e=>(string?)e.Attribute("PartName")=="/"+media))
            types.Root.Add(new XElement(CT+"Override",Attr("PartName","/"+media),Attr("ContentType",asset.GetProperty("mime").GetString()!)));
        parts[media]=data;parts[relName]=Encoding.UTF8.GetBytes(rels.ToString(SaveOptions.DisableFormatting));parts["[Content_Types].xml"]=Encoding.UTF8.GetBytes(types.ToString(SaveOptions.DisableFormatting));
        return rid;
    }
    internal static bool Replace(Dictionary<string,byte[]> parts,string mainName,W.Drawing drawing,JsonElement asset)
    {
        var (_,_,picture)=ImageFormatting.Parts(drawing);
        var blip=picture.BlipFill!.Blip!;
        // Alternate vector/extension image sources could override the replaced bitmap.
        if(blip.ChildElements.Count!=0)throw new InvalidDataException("Image effects or alternate sources require explicit replacement support");
        var rels=Xml(parts[Relationships(mainName)]);
        var relation=rels.Root!.Elements(REL+"Relationship").SingleOrDefault(e=>(string?)e.Attribute("Id")==blip.Embed!.Value);
        if(relation==null || (string?)relation.Attribute("Type")!=R.NamespaceName+"/image" || (string?)relation.Attribute("TargetMode")=="External")
            throw new InvalidDataException("Unresolved embedded image relationship");
        var uri=new Uri(new Uri("http://package/"+mainName),(string?)relation.Attribute("Target") ?? "");
        var previous=Uri.UnescapeDataString(uri.AbsolutePath).TrimStart('/');
        if(!parts.TryGetValue(previous,out var old))throw new InvalidDataException("Missing embedded image part");
        if(Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(old)).ToLowerInvariant()==asset.GetProperty("hash").GetString())return false;
        blip.Embed=Embed(parts,mainName,asset);
        // Keep the old relationship/media: other pictures may still reference them.
        return true;
    }
    internal static List<W.Paragraph> Insert(Dictionary<string,byte[]> parts,string mainName,OpenXmlElement target,JsonElement op,JsonElement asset)
    {
        if(target is not W.Paragraph || (target.Parent is not W.Body && target.Parent is not W.TableCell))
            throw new InvalidDataException("Image insertion requires a body or table-cell paragraph anchor");
        var rid=Embed(parts,mainName,asset);
        uint id=1;
        foreach(var entry in parts.Where(kv=>kv.Key.EndsWith(".xml")))
            foreach(var p in Xml(entry.Value).Descendants(WP+"docPr"))
                if(uint.TryParse((string?)p.Attribute("id"),out var used) && used>=id)id=checked(used+1);
        decimal inches=op.GetProperty("width_inches").GetDecimal();
        if(inches<.1m || inches>22)throw new InvalidDataException("Image width outside bounds");
        long cx=(long)decimal.Round(inches*914400,0,MidpointRounding.AwayFromZero);
        long cy=(long)decimal.Round(cx*(decimal)asset.GetProperty("height").GetInt32()/asset.GetProperty("width").GetInt32(),0,MidpointRounding.AwayFromZero);
        if(cy<1 || cy>22*914400)throw new InvalidDataException("Image aspect ratio exceeds placement height bounds");
        var drawing=new XElement(WN+"drawing",new XAttribute(XNamespace.Xmlns+"w",WN),new XAttribute(XNamespace.Xmlns+"wp",WP),new XAttribute(XNamespace.Xmlns+"a",A),new XAttribute(XNamespace.Xmlns+"pic",PIC),new XAttribute(XNamespace.Xmlns+"r",R),
            new XElement(WP+"inline",Attr("distT",0),Attr("distB",0),Attr("distL",0),Attr("distR",0),
                new XElement(WP+"extent",Attr("cx",cx),Attr("cy",cy)),
                new XElement(WP+"docPr",Attr("id",id),Attr("name","LocalOffice image "+id),Attr("descr",op.GetProperty("alt_text").GetString()!)),
                new XElement(WP+"cNvGraphicFramePr",new XElement(A+"graphicFrameLocks",Attr("noChangeAspect",1))),
                new XElement(A+"graphic",new XElement(A+"graphicData",Attr("uri",PIC.NamespaceName),new XElement(PIC+"pic",
                    new XElement(PIC+"nvPicPr",new XElement(PIC+"cNvPr",Attr("id",id),Attr("name","Image "+id)),new XElement(PIC+"cNvPicPr")),
                    new XElement(PIC+"blipFill",new XElement(A+"blip",new XAttribute(R+"embed",rid)),new XElement(A+"stretch",new XElement(A+"fillRect"))),
                    new XElement(PIC+"spPr",new XElement(A+"xfrm",new XElement(A+"off",Attr("x",0),Attr("y",0)),new XElement(A+"ext",Attr("cx",cx),Attr("cy",cy))),new XElement(A+"prstGeom",Attr("prst","rect"),new XElement(A+"avLst"))))))));
        var align=op.GetProperty("alignment").GetString()!;
        var paragraph=new W.Paragraph(new W.ParagraphProperties(new W.Justification { Val=align switch {"left"=>W.JustificationValues.Left,"center"=>W.JustificationValues.Center,"right"=>W.JustificationValues.Right,_=>throw new InvalidDataException("Invalid alignment") }}),new W.Run(new W.Drawing(drawing.ToString(SaveOptions.DisableFormatting))));
        if(op.GetProperty("position").GetString()=="before")target.InsertBeforeSelf(paragraph);else target.InsertAfterSelf(paragraph);
        var added=new List<W.Paragraph>{paragraph};
        if(op.TryGetProperty("caption",out var caption) && caption.ValueKind==JsonValueKind.String)
        {
            paragraph.ParagraphProperties!.AddChild(new W.KeepNext());
            var p=new W.Paragraph(new W.ParagraphProperties(new W.Justification { Val=paragraph.ParagraphProperties.Justification!.Val }),new W.Run(new W.Text(caption.GetString()!){Space=SpaceProcessingModeValues.Preserve}));
            paragraph.InsertAfterSelf(p);added.Add(p);
        }
        return added;
    }
}
