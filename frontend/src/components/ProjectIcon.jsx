import React from "react";
import {Sparkles} from "lucide-react";

export default function ProjectIcon({size=20,className=""}){
 return <span className={"project-icon "+className} aria-label="Personal AI"><Sparkles size={size} strokeWidth={2.2}/></span>;
}
