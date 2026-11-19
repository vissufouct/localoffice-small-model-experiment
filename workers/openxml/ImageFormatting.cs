using System.Globalization;
using System.Text.Json;
using DocumentFormat.OpenXml;
using W=DocumentFormat.OpenXml.Wordprocessing;
using D=DocumentFormat.OpenXml.Drawing;
using DW=DocumentFormat.OpenXml.Drawing.Wordprocessing;
using P=DocumentFormat.OpenXml.Drawing.Pictures;

static class ImageFormatting
{
    internal static (DW.Extent extent,DW.DocProperties description,P.Picture picture) Parts(W.Drawing drawing)
    {
        if(drawing.ChildElements.Count!=1 || (drawing.FirstChild is not DW.Inline && drawing.FirstChild is not DW.Anchor))
            throw new InvalidDataException("Image formatting requires one inline or anchored picture");
        var pictures=drawing.Descendants<P.Picture>().ToList();var extents=drawing.Descendants<DW.Extent>().ToList();var props=drawing.Descendants<DW.DocProperties>().ToList();
        if(pictures.Count!=1 || extents.Count!=1 || props.Count!=1 || pictures[0].Parent is not D.GraphicData)
            throw new InvalidDataException("Grouped or multiple-picture drawing is not supported for image formatting");
        var blip=pictures[0].BlipFill?.Blip;
        if(blip?.Embed?.Value==null || blip.Link?.Value!=null)
            throw new InvalidDataException("Image formatting requires an embedded picture");
        return (extents[0],props[0],pictures[0]);
    }

    static decimal Crop(D.SourceRectangle? rect,string edge)
    {
        var raw=rect?.GetAttributes().FirstOrDefault(a=>a.LocalName==edge && a.NamespaceUri=="").Value;
        if(string.IsNullOrEmpty(raw))return 0;
        bool percent=raw.EndsWith('%');
        if(!decimal.TryParse(percent?raw[..^1]:raw,NumberStyles.Float,CultureInfo.InvariantCulture,out var value))
            throw new InvalidDataException("Unresolved crop percentage");
        return percent?value:value/1000;
    }

    internal static object Describe(W.Drawing drawing)
    {
        try {
            var (extent,description,picture)=Parts(drawing);var crop=picture.BlipFill?.GetFirstChild<D.SourceRectangle>();
            var alt=description.Description?.Value ?? "";
            return new { supported=true,kind=drawing.FirstChild is DW.Inline?"inline":"anchor",
                layout=ImageLayout.Describe(drawing),
                properties=new { width_inches=extent.Cx?.Value/914400m,height_inches=extent.Cy?.Value/914400m,
                    crop_left_percent=Crop(crop,"l"),crop_top_percent=Crop(crop,"t"),crop_right_percent=Crop(crop,"r"),crop_bottom_percent=Crop(crop,"b"),alt_text=alt[..Math.Min(alt.Length,1000)] },
                alt_text_truncated=alt.Length>1000,interpretation="Direct frame geometry; crop percentages are source-edge offsets. Image bytes and wrapping are unchanged by format_image." };
        } catch(InvalidDataException error) { return new { supported=false,reason=error.Message }; }
    }

    internal static void Apply(W.Drawing drawing,JsonElement properties)
    {
        var (extent,description,picture)=Parts(drawing);
        bool Has(string name)=>properties.TryGetProperty(name,out var value) && value.ValueKind!=JsonValueKind.Null;
        if(Has("width_inches") || Has("height_inches"))
        {
            long oldX=extent.Cx?.Value ?? 0,oldY=extent.Cy?.Value ?? 0;
            var transform=picture.ShapeProperties?.GetFirstChild<D.Transform2D>();var inner=transform?.Extents;
            if(oldX<=0 || oldY<=0 || inner?.Cx?.Value!=oldX || inner?.Cy?.Value!=oldY || drawing.Descendants().Any(e=>e.LocalName is "sizeRelH" or "sizeRelV"))
                throw new InvalidDataException("Unresolved or relative image dimensions; preserve until explicit geometry support");
            decimal width=Has("width_inches")?properties.GetProperty("width_inches").GetDecimal():oldX/914400m;
            decimal height=Has("height_inches")?properties.GetProperty("height_inches").GetDecimal():oldY/914400m;
            if(!Has("height_inches"))height=width*oldY/oldX;
            if(!Has("width_inches"))width=height*oldX/oldY;
            if(width<.1m || height<.1m || width>22 || height>22)throw new InvalidDataException("Image frame must be 0.1–22 inches on each axis");
            long x=(long)decimal.Round(width*914400,0,MidpointRounding.AwayFromZero),y=(long)decimal.Round(height*914400,0,MidpointRounding.AwayFromZero);
            if(x!=oldX || y!=oldY) { extent.Cx=x;extent.Cy=y;inner!.Cx=x;inner.Cy=y; }
        }
        var names=new[]{"crop_left_percent","crop_top_percent","crop_right_percent","crop_bottom_percent"};
        var edges=new[]{"l","t","r","b"};
        if(names.Any(Has))
        {
            var fill=picture.BlipFill!;
            if(fill.GetFirstChild<D.Stretch>()==null || fill.GetFirstChild<D.Tile>()!=null)
                throw new InvalidDataException("Crop editing requires stretch-filled image content");
            var rect=fill.GetFirstChild<D.SourceRectangle>();var values=edges.Select(e=>Crop(rect,e)).ToArray();
            for(int i=0;i<4;i++)if(Has(names[i]))values[i]=properties.GetProperty(names[i]).GetDecimal();
            if(values.Any(v=>v<0 || v>=100 || v*1000!=decimal.Truncate(v*1000)) || values[0]+values[2]>=100 || values[1]+values[3]>=100)
                throw new InvalidDataException("Crop must leave a positive visible source area, with 0.001 percent precision");
            for(int i=0;i<4;i++)if(Has(names[i]) && values[i]!=Crop(rect,edges[i]))
            {
                if(rect==null) { rect=new D.SourceRectangle();fill.AddChild(rect); }
                rect.SetAttribute(new OpenXmlAttribute(edges[i],"",decimal.ToInt32(values[i]*1000).ToString(CultureInfo.InvariantCulture)));
            }
        }
        if(Has("alt_text") && description.Description?.Value!=properties.GetProperty("alt_text").GetString())
            description.Description=properties.GetProperty("alt_text").GetString();
    }
}
