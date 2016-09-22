<?xml version="1.0" encoding="utf-8"?>
<!--
Note: this template expects XHTML1.1, outputs HTML5

It's a generic template for any kind of content
-->
<xsl:stylesheet version="1.0"
		xmlns:xhtml="http://www.w3.org/1999/xhtml"
		xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
		xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
		xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
		xmlns:dcterms="http://purl.org/dc/terms/"
		xmlns:prov="http://www.w3.org/ns/prov#"
		xmlns:rinfo="http://rinfo.lagrummet.se/taxo/2007/09/rinfo/pub#"
		xmlns:rinfoex="http://lagen.nu/terms#"
		xml:space="preserve"
		exclude-result-prefixes="xhtml rdf rdfs prov">

  <xsl:import href="uri.xsl"/>
  <xsl:include href="base.xsl"/>

  <!-- Implementations of templates called by base.xsl -->
  <xsl:template name="headtitle"><xsl:value-of select="//xhtml:title"/> | <xsl:value-of select="$configuration/sitename"/></xsl:template>
  <xsl:template name="metarobots"/>
  <xsl:template name="linkalternate"/>
  <xsl:template name="headmetadata"/>
  <xsl:template name="bodyclass">forarbete</xsl:template>
  <xsl:template name="pagetitle">
    <div class="row toplevel">
      <section class="col-sm-8">
	<p style="font-size: 24pt;"><xsl:value-of select="../xhtml:head/xhtml:meta[@property='dcterms:identifier']/@content"/></p>
	<p style="font-size: 20pt;"><xsl:value-of select="../xhtml:head/xhtml:title"/></p>
      </section>
      <aside class="source col-sm-4">
	<xsl:variable name="docuri" select="@about"/>
	<xsl:variable name="derivedfrom" select="$annotations/resource[@uri=$docuri]/prov:wasDerivedFrom/@ref"/>
	Originaldokument: <a href="{$derivedfrom}"><xsl:value-of select="$annotations/resource[@uri=$derivedfrom]/rdfs:label"/></a>, <a href="{$annotations/resource[@uri=$docuri]/prov:alternateOf/@ref}">Källa</a>
      </aside>
    </div>
  </xsl:template>
  <xsl:param name="dyntoc" select="true()"/>
  <xsl:param name="content-under-pagetitle" select="false()"/>

  <!-- Headings shouldn't be expressed with <h*> tags, but rather with
       RDFa attribs in <div class="section"> element. However,
       DirTrips still generates h1 headings, so we can't just ignore
       these. -->
  <!-- <xsl:template match="xhtml:h1|xhtml:h2"/> -->
  
  <xsl:template match="xhtml:a">
    <xsl:call-template name="link"/>
  </xsl:template>

  <xsl:template name="aside-annotations">
    <xsl:param name="uri"/>
    <xsl:if test="$annotations/resource[@uri=$uri]">
      <div class="col-sm-4">
	<h2>Annotations for <xsl:value-of select="substring-after($uri,'http://localhost:8000/res/')"/></h2>
	<xsl:for-each select="$annotations/resource[@uri=$uri]/dcterms:isReferencedBy">
	  <xsl:variable name="referencing" select="@ref"/>
	  <a href="{@ref}"><xsl:value-of select="$annotations/resource[@uri=$referencing]/dcterms:identifier"/></a>
	</xsl:for-each>
      </div>
    </xsl:if>
  </xsl:template>

  <xsl:template match="xhtml:body/xhtml:div">
    <div class="row toplevel">
      <section id="{substring-after(@about,'#')}" class="col-sm-8">
	<xsl:apply-templates select="*[not(xhtml:div[@about])]"/>
      </section>
      <!--
      <xsl:call-template name="aside-annotations">
	<xsl:with-param name="uri" select="../@about"/>
	</xsl:call-template>
	-->
    </div>
  </xsl:template>
    
  <!-- everything that has an @about attribute, i.e. _is_ something
       (with a URI) gets a <section> with an <aside> for inbound links etc -->
  <xsl:template match="xhtml:div[@about and @class='section']">
    <div class="row" about="{@about}"><!-- needed? -->
      <section id="{substring-after(@about,'#')}" class="col-sm-8">
	<xsl:variable name="sectionheading"><xsl:if test="xhtml:span/@content"><xsl:value-of select="xhtml:span/@content"/>. </xsl:if><xsl:value-of select="@content"/></xsl:variable>
	<xsl:if test="count(ancestor::*) = 2">
	    <h2><xsl:value-of select="$sectionheading"/></h2>
	</xsl:if>
	<xsl:if test="count(ancestor::*) = 3">
	  <h3><xsl:value-of select="$sectionheading"/></h3>
	</xsl:if>
	<xsl:if test="count(ancestor::*) = 4">
	  <h4><xsl:value-of select="$sectionheading"/></h4>
	</xsl:if>
       <xsl:apply-templates select="*[not(@about and @class!='forfattningskommentar')]"/>
      </section>
      <xsl:call-template name="aside-annotations">
	<xsl:with-param name="uri" select="@about"/>
      </xsl:call-template>
    </div>
    <!--
    We handle all @about sections afterwards the rest to flatten out sections, ie from
    a structure like:

    4
      4.1
        4.1.1
        4.1.2

    we produce
         
    4
    4.1
    4.1.1
    4.1.2


   This only works when a @about sections only have other @about
   sections as direct descendents, or it has no @about sections as
   direct descendents. For forfattningskommentar subsections, this
   does not hold.
    -->
         	 
    <xsl:comment>docpart level: subparts start</xsl:comment>
    <xsl:apply-templates select="xhtml:div[@about and @class!='forfattningskommentar']"/>
    <xsl:comment>docpart level: subparts end</xsl:comment>
  </xsl:template>


  <xsl:template match="xhtml:div[@about and @class='forfattningskommentar']">
    <xsl:if test="string-length(@content) > 0">
      <h3><xsl:value-of select="@content"/></h3>
    </xsl:if>
    <div class="forfattningskommentar" id="{substring-after(@about, '#')}">
       <xsl:apply-templates select="xhtml:div/xhtml:div/*"/>
    </div>
  </xsl:template>

  <!-- remove prop{rubrik,huvudrubrik} as they are duplicates of what occurs in pagetitle -->
  <xsl:template match="xhtml:h1[@class='prophuvudrubrik' or @class='proprubrik']"/>
  
  <!-- remove spans which only purpose is to contain RDFa data -->
  <xsl:template match="xhtml:span[@property and @content and not(text())]"/>

  <xsl:template match="xhtml:div[@about]" mode="toc"/>

  <xsl:template match="xhtml:span[@class='sidbrytning']">
    <div class="sida" id="{@id}">
      <p class="sidbrytning"><i>Sida <xsl:value-of select="substring(@id,4)"/></i>
      <button type="button" class="view-facsimile pull-left">
 	<span>Visa faksimil</span>
 	<span style="display: none">Visa text</span>
      </button>
      <img data-src="{@src}" class="facsimile"/>
      </p>
    </div>
  </xsl:template>


  <!-- remove these empty elements (often <i/> or <span/> tags) -->
  <xsl:template match="xhtml:span|xhtml:i[not(text())]">
  </xsl:template>
  <!-- default template: translate everything from whatever namespace
       it's in (usually the XHTML1.1 NS) into the default namespace
       NOTE: It removes any attributes not accounted for otherwise
       -->
  <xsl:template match="*">
    <xsl:element name="{local-name(.)}"><xsl:apply-templates select="node()"/></xsl:element>
  </xsl:template>

  <xsl:template match="@*">
    <xsl:attribute name="{local-name(.)}"><xsl:apply-templates select="@*"/></xsl:attribute>
  </xsl:template>

  <!-- alternatively: identity transform (keep source namespace) -->
  <!--
  <xsl:template match="@*|node()">
    <xsl:copy>
      <xsl:apply-templates select="@*|node()"/>
    </xsl:copy>
  </xsl:template>
  --> 
  <!-- toc handling (do nothing) -->
  <xsl:template match="@*|node()" mode="toc"/>
  
</xsl:stylesheet>
