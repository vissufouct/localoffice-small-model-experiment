using DocumentFormat.OpenXml;
using W = DocumentFormat.OpenXml.Wordprocessing;

static class Lists
{
    static W.AbstractNum? Definition(W.NumberingInstance n) => (n.Parent as W.Numbering)?.Elements<W.AbstractNum>().FirstOrDefault(a=>a.AbstractNumberId?.Value==n.AbstractNumId?.Val?.Value);
    static W.Level? Level(W.NumberingInstance n,int index) => n.Elements<W.LevelOverride>().FirstOrDefault(o=>o.LevelIndex?.Value==index)?.Level ?? Definition(n)?.Elements<W.Level>().FirstOrDefault(l=>l.LevelIndex?.Value==index);
    internal static string Preview(W.NumberingInstance n) => "number_id="+n.NumberID?.Value+" "+string.Join(", ",Enumerable.Range(0,9).Select(i=>(index:i,level:Level(n,i))).Where(x=>x.level!=null).Select(x=>"level "+x.index+": "+x.level!.NumberingFormat?.Val?.InnerText+" "+x.level.LevelText?.Val?.Value));
    internal static object Describe(W.NumberingInstance n) => new {
        supported=true,number_id=n.NumberID?.Value,abstract_id=n.AbstractNumId?.Val?.Value,
        interpretation="Existing list definition; reuse its ID to continue numbering. Marker text is not paragraph content. Style-linked definitions require explicit resolution.",
        style_linked=Definition(n)?.NumberingStyleLink!=null || Definition(n)?.StyleLink!=null,
        levels=Enumerable.Range(0,9).Select(i=>(index:i,level:Level(n,i))).Where(x=>x.level!=null).Select(x=>new {
            level=x.index,format=x.level!.NumberingFormat?.Val?.InnerText,marker=x.level.LevelText?.Val?.Value,
            start=n.Elements<W.LevelOverride>().FirstOrDefault(o=>o.LevelIndex?.Value==x.index)?.StartOverrideNumberingValue?.Val?.Value ?? x.level.StartNumberingValue?.Val?.Value,
            paragraph_style=x.level.ParagraphStyleIdInLevel?.Val?.Value }) };

    static void Check(bool condition,string message)
    {
        if(!condition)throw new InvalidDataException(message);
    }

    internal static int Restart(W.Paragraph paragraph,W.Numbering? numbering,int numberId,int level,int start)
    {
        Check(numberId>0 && level==0 && start>=1 && start<=32767,"Restart requires a positive list ID, level 0 and start 1–32767");
        Check(numbering!=null,"Restart requires an existing numbering part");
        var instances=numbering!.Elements<W.NumberingInstance>().ToList();
        Check(instances.All(n=>n.NumberID?.Value>0) && instances.Select(n=>n.NumberID!.Value).Distinct().Count()==instances.Count,"Ambiguous numbering instance IDs");
        var instance=instances.SingleOrDefault(n=>n.NumberID!.Value==numberId);
        Check(instance!=null,"Unknown list definition; discover numbering IDs first");
        Check(instance!.ChildElements.All(e=>e is W.AbstractNumId || e is W.LevelOverride) && instance.Elements<W.AbstractNumId>().Count()==1,"Unsupported numbering instance semantics");
        var definitions=numbering.Elements<W.AbstractNum>().Where(a=>a.AbstractNumberId?.Value==instance.AbstractNumId?.Val?.Value).ToList();
        Check(definitions.Count==1,"Unresolved or ambiguous abstract numbering definition");
        var definition=definitions[0];
        Check(definition.NumberingStyleLink==null && definition.StyleLink==null,"Style-linked numbering requires explicit resolution");
        Check(definition.MultiLevelType==null || definition.MultiLevelType.Val?.Value==W.MultiLevelValues.SingleLevel,"Restart supports single-level numbering only");
        var levels=definition.Elements<W.Level>().ToList();
        Check(levels.Count==1 && levels[0].LevelIndex?.Value==0,"Restart supports a single explicit level 0 only");
        var overrides=instance.Elements<W.LevelOverride>().ToList();
        Check(overrides.Count<=1 && overrides.All(o=>o.LevelIndex?.Value==0),"Restart does not support multilevel overrides");
        foreach(var o in overrides)
        {
            Check(o.ChildElements.All(e=>e is W.StartOverrideNumberingValue || e is W.Level)
                && o.Elements<W.Level>().Count()<=1 && o.Elements<W.StartOverrideNumberingValue>().Count()<=1,"Unsupported level override semantics");
            Check(o.Level==null || o.Level.LevelIndex?.Value==0,"Override level must match level 0");
        }
        // Validate both the base and any replacement level: do not silently inherit
        // a bullet/linked/restarting level through an otherwise decimal instance.
        foreach(var l in levels.Concat(overrides.Where(o=>o.Level!=null).Select(o=>o.Level!)))
        {
            Check(l.NumberingFormat?.Val?.Value==W.NumberFormatValues.Decimal && l.LevelText?.Val?.Value!=null,"Restart requires explicit decimal format and marker");
            var marker=l.LevelText!.Val!.Value!;
            Check(marker.Contains("%1") && !System.Text.RegularExpressions.Regex.IsMatch(marker,@"%(?!1)"),"Unresolved or multilevel numbering marker");
            Check(l.ParagraphStyleIdInLevel==null && l.LevelRestart==null && l.IsLegalNumberingStyle==null && l.LevelPictureBulletId==null,"Unsupported linked or restart level semantics");
        }
        Check(!paragraph.Descendants().Any(e=>e.LocalName is "pPrChange" or "numberingChange" or "ins" or "del" or "moveFrom" or "moveTo")
            && !paragraph.Ancestors().Any(e=>e.LocalName is "ins" or "del" or "moveFrom" or "moveTo"),"Tracked paragraph numbering cannot be restarted");
        var current=paragraph.ParagraphProperties?.NumberingProperties;
        Check(current==null || current.ChildElements.All(e=>e is W.NumberingId || e is W.NumberingLevelReference),"Tracked list properties require preservation-aware editing");
        var max=instances.Max(n=>n.NumberID!.Value);
        Check(max<int.MaxValue,"No available numbering instance ID");
        // A fresh numId sharing the abstract definition can be merged into the
        // original sequence by renderers. Isolate the abstract identity as well.
        var abstracts=numbering.Elements<W.AbstractNum>().ToList();
        Check(abstracts.All(a=>a.AbstractNumberId?.Value>=0)
            && abstracts.Select(a=>a.AbstractNumberId!.Value).Distinct().Count()==abstracts.Count,"Ambiguous abstract numbering IDs");
        var abstractMax=abstracts.Max(a=>a.AbstractNumberId!.Value);
        Check(abstractMax<int.MaxValue,"No available abstract numbering ID");
        var independent=(W.AbstractNum)definition.CloneNode(true);
        independent.AbstractNumberId=abstractMax+1;
        // nsid identifies a numbering definition across imports; do not retain
        // the source identity even when its formatting is an exact clone.
        var identities=abstracts.Select(a=>a.Nsid?.Val?.Value).ToHashSet(StringComparer.OrdinalIgnoreCase);
        string identity;
        do { identity=Guid.NewGuid().ToString("N")[..8].ToUpperInvariant(); } while(identities.Contains(identity));
        independent.Nsid=new W.Nsid { Val=identity };
        numbering.InsertAfter(independent,abstracts.Last());
        var fresh=(W.NumberingInstance)instance.CloneNode(true);fresh.NumberID=max+1;
        fresh.AbstractNumId=new W.AbstractNumId { Val=abstractMax+1 };
        var change=fresh.Elements<W.LevelOverride>().SingleOrDefault();
        if(change==null){change=new W.LevelOverride { LevelIndex=0 };fresh.Append(change);}
        change.StartOverrideNumberingValue=new W.StartOverrideNumberingValue { Val=start };
        // Insert after the last instance, before any trailing cleanup metadata.
        // AddChild replaces existing instances for this SDK particle; never use it here.
        numbering.InsertAfter(fresh,instances.Last());
        Apply(paragraph,numbering,max+1,0);
        return max+1;
    }

    internal static void Apply(W.Paragraph paragraph,W.Numbering? numbering,int numberId,int level)
    {
        if(numberId<0 || level<0 || level>8 || (numberId==0 && level!=0))throw new InvalidDataException("Invalid list ID/level");
        if(numberId!=0)
        {
            var instance=numbering?.Elements<W.NumberingInstance>().SingleOrDefault(n=>n.NumberID?.Value==numberId) ?? throw new InvalidDataException("Unknown list definition; discover numbering IDs first");
            var definition=Definition(instance);
            if(definition?.NumberingStyleLink!=null || definition?.StyleLink!=null)throw new InvalidDataException("Style-linked numbering requires explicit resolution");
            var chosen=Level(instance,level) ?? throw new InvalidDataException("Requested level is not defined in this list");
            if(chosen.NumberingFormat==null || chosen.LevelText==null)throw new InvalidDataException("List level has unresolved format or marker");
        }
        var existing=paragraph.ParagraphProperties?.NumberingProperties;
        if(existing!=null && existing.ChildElements.Any(e=>e is not W.NumberingId && e is not W.NumberingLevelReference))throw new InvalidDataException("Tracked list properties require preservation-aware editing");
        paragraph.ParagraphProperties ??= new W.ParagraphProperties();
        // Zero explicitly disables style-inherited numbering instead of just removing numPr.
        var properties=new W.NumberingProperties();
        if(numberId!=0)properties.Append(new W.NumberingLevelReference { Val=level });
        properties.Append(new W.NumberingId { Val=numberId });
        paragraph.ParagraphProperties.NumberingProperties=properties;
    }
}
